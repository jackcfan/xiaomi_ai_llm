import json
import unittest

import httpx

from hub.llm import ChatClient, LLMError


def sse(*deltas, done=True):
    lines = []
    for d in deltas:
        payload = {"choices": [{"index": 0, "delta": {"content": d}}]}
        lines.append("data: " + json.dumps(payload, ensure_ascii=False))
        lines.append("")
    if done:
        lines.append("data: [DONE]")
        lines.append("")
    return ("\n".join(lines) + "\n").encode("utf-8")


class TrackingStream(httpx.AsyncByteStream):
    """Yields one chunk then would block forever; records aclose."""

    def __init__(self, first: bytes):
        self.first = first
        self.closed = False

    async def __aiter__(self):
        yield self.first
        import asyncio
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class ChatClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_streams_content_and_sends_messages(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                content=sse("你好", "。"),
                headers={"Content-Type": "text/event-stream"},
            )

        client = ChatClient("https://api.example.com/v1/", "k", "m1",
                            temperature=0.5, max_tokens=100,
                            transport=httpx.MockTransport(handler))
        history = [{"role": "user", "content": "q0"}, {"role": "assistant", "content": "a0"}]
        out = []
        async for tok in client.chat_stream(message="hi", system_prompt="S",
                                            conversation_history=history):
            out.append(tok)

        self.assertEqual("".join(out), "你好。")
        self.assertEqual(seen["url"], "https://api.example.com/v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer k")
        body = seen["body"]
        self.assertEqual(body["model"], "m1")
        self.assertIs(body["stream"], True)
        self.assertEqual(body["temperature"], 0.5)
        self.assertEqual(body["max_tokens"], 100)
        self.assertEqual(body["messages"][0], {"role": "system", "content": "S"})
        self.assertEqual(body["messages"][1:3], history)
        self.assertEqual(body["messages"][-1], {"role": "user", "content": "hi"})

    async def test_optional_fields_omitted_and_empty_deltas_skipped(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            raw = (
                b": keep-alive\n\n"
                b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":""}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":null}}]}\n\n'
                b'data: {"choices":[]}\n\n'
                b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                b"data: [DONE]\n\n"
                b'data: {"choices":[{"delta":{"content":"after"}}]}\n\n'
            )
            return httpx.Response(200, content=raw)

        client = ChatClient("https://api.example.com", "k", "m",
                            transport=httpx.MockTransport(handler))
        out = [t async for t in client.chat_stream(message="hi")]
        self.assertEqual(out, ["ok"])
        body = seen["body"]
        self.assertNotIn("temperature", body)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])

    async def test_extra_body_merged_into_request(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, content=sse("ok"))

        client = ChatClient(
            "https://api.example.com", "k", "m1", temperature=0.3,
            transport=httpx.MockTransport(handler),
            extra_body={
                "thinking": {"type": "disabled"},
                "model": "hijack", "stream": False, "messages": [], "temperature": 1.5,
            },
        )
        out = [t async for t in client.chat_stream(message="hi")]
        self.assertEqual(out, ["ok"])
        body = seen["body"]
        self.assertEqual(body["thinking"], {"type": "disabled"})
        # 核心字段与显式参数优先，extra_body 不能覆盖
        self.assertEqual(body["model"], "m1")
        self.assertIs(body["stream"], True)
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(body["temperature"], 0.3)

    async def test_no_extra_body_sends_no_thinking(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, content=sse("ok"))

        client = ChatClient("https://api.example.com", "k", "m",
                            transport=httpx.MockTransport(handler))
        [t async for t in client.chat_stream(message="hi")]
        self.assertNotIn("thinking", seen["body"])

    async def test_http_error_raises(self):
        def handler(request):
            return httpx.Response(401, text="invalid api key provided: secret-key-123" + "x" * 500)

        client = ChatClient("https://api.example.com/v1", "secret-key-123", "m",
                            transport=httpx.MockTransport(handler))
        with self.assertRaises(LLMError) as cm, self.assertLogs("hub.llm", "WARNING"):
            async for _ in client.chat_stream(message="hi"):
                pass
        err = cm.exception
        self.assertEqual(err.status, 401)
        self.assertLessEqual(len(err.body), 200)
        self.assertNotIn("secret-key-123", str(err))

    async def test_aclose_closes_http_stream(self):
        stream = TrackingStream(sse("a", done=False))

        def handler(request):
            return httpx.Response(200, stream=stream)

        client = ChatClient("https://api.example.com/v1", "k", "m",
                            transport=httpx.MockTransport(handler))
        gen = client.chat_stream(message="hi")
        self.assertEqual(await gen.__anext__(), "a")
        await gen.aclose()
        self.assertTrue(stream.closed)

    async def test_logs_model_call_and_done_status(self):
        def handler(request):
            return httpx.Response(200, content=sse("hi", "。"))

        client = ChatClient("https://api.example.com/v1", "k", "m1",
                            transport=httpx.MockTransport(handler))
        with self.assertLogs("hub.llm", "INFO") as logs:
            [t async for t in client.chat_stream(message="hello")]
        out = "\n".join(logs.output)
        self.assertIn("call model=m1", out)
        self.assertIn("done model=m1", out)
        self.assertIn("status=200", out)
        self.assertIn("chars=3", out)  # "hi。" 共 3 字符

    async def test_logs_model_on_http_error(self):
        def handler(request):
            return httpx.Response(401, text="bad key sk-secret")

        client = ChatClient("https://api.example.com/v1", "sk-secret", "m2",
                            transport=httpx.MockTransport(handler))
        with self.assertLogs("hub.llm", "WARNING") as logs:
            with self.assertRaises(LLMError):
                [t async for t in client.chat_stream(message="hi")]
        out = "\n".join(logs.output)
        self.assertIn("m2", out)
        self.assertNotIn("sk-secret", out)


if __name__ == "__main__":
    unittest.main()
