import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx

from hub import web
from hub.web import WebResult, ground

PROVIDER = {"base_url": "https://api.deepseek.com", "api_key": "sk-secret-key", "model": "deepseek-chat"}
NOW = datetime(2026, 11, 22, 9, 30, tzinfo=timezone(timedelta(hours=8)))


def plan_response(plan: dict, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{
            "index": 0,
            "finish_reason": finish_reason,
            "message": {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)},
        }]
    })


class Router:
    """MockTransport handler that routes by URL path and records requests."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[httpx.Request] = []

    def paths(self) -> list[str]:
        return [r.url.path for r in self.calls]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        handler = self.routes.get(request.url.path)
        if handler is None:
            return httpx.Response(404, json={"error": "no route"})
        return handler(request) if callable(handler) else handler

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


SEARCH_RESULT = {
    "title": "DeepSeek 发布新模型",
    "url": "https://news.example.com/deepseek-v5",
    "date": "2026-11-21",
    "summary": "DeepSeek 于 11 月 21 日发布新模型。",
}


class GroundTest(unittest.IsolatedAsyncioTestCase):
    async def test_plan_answer_needs_no_tools(self):
        router = Router({"/chat/completions": plan_response({"action": "answer", "query": "", "url": ""})})
        result = await ground("写一首诗", [], PROVIDER, transport=router.transport(), now=NOW)
        self.assertEqual(result, WebResult(action="answer", evidence_messages=[], sources=[]))
        self.assertEqual(router.paths(), ["/chat/completions"])

    async def test_planner_request_matches_device(self):
        router = Router({"/chat/completions": plan_response({"action": "answer"})})
        history = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好呀"}]
        await ground("今天金价多少", history, PROVIDER, transport=router.transport(), now=NOW)
        req = router.calls[0]
        self.assertEqual(str(req.url), "https://api.deepseek.com/chat/completions")
        self.assertEqual(req.headers["Authorization"], "Bearer sk-secret-key")
        body = json.loads(req.content)
        self.assertEqual(body["model"], "deepseek-chat")
        self.assertFalse(body["stream"])
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["max_tokens"], 400)
        system, user = body["messages"]
        self.assertEqual(system["role"], "system")
        self.assertTrue(system["content"].startswith("你是语音助手的联网路由器。"))
        self.assertTrue(system["content"].endswith("当前时间：2026-11-22 09:30 +0800"))
        self.assertEqual(json.loads(user["content"]), history + [{"role": "user", "content": "今天金价多少"}])

    async def test_plan_search_builds_evidence(self):
        router = Router({
            "/chat/completions": plan_response({"action": "search", "query": "DeepSeek 最新模型 2026年11月"}),
            "/v1/tools/search": httpx.Response(200, json={"results": [SEARCH_RESULT]}),
        })
        with self.assertLogs("hub.web", level="INFO") as logs:
            result = await ground("DeepSeek 最近发布了什么", [], PROVIDER, transport=router.transport(), now=NOW)
        self.assertEqual(result.action, "search")
        self.assertEqual(len(result.evidence_messages), 2)
        system, evidence = result.evidence_messages
        self.assertEqual(system["role"], "system")
        self.assertTrue(system["content"].startswith("当前时间：2026-11-22 09:30 +0800。最后一个用户问题已完成联网检索。"))
        self.assertEqual(evidence["role"], "user")
        self.assertTrue(evidence["content"].startswith("外部参考资料："))
        self.assertIn(SEARCH_RESULT["title"], evidence["content"])
        self.assertEqual(result.sources, [SEARCH_RESULT["url"]])
        search_req = router.calls[1]
        self.assertEqual(str(search_req.url), "https://api.deepseek.com/v1/tools/search")
        self.assertEqual(json.loads(search_req.content),
                         {"query": "DeepSeek 最新模型 2026年11月", "count": 3, "need_content": False})
        self.assertNotIn("sk-secret-key", "\n".join(logs.output))

    async def test_on_tool_start_fires_before_tool_request_for_search_only(self):
        events = []

        def search_handler(request):
            events.append("tool_request")
            return httpx.Response(200, json={"results": [SEARCH_RESULT]})

        router = Router({
            "/chat/completions": plan_response({"action": "search", "query": "DeepSeek 最新模型"}),
            "/v1/tools/search": search_handler,
        })
        with self.assertLogs("hub.web", level="INFO"):
            result = await ground("DeepSeek 最近发布了什么", [], PROVIDER, transport=router.transport(),
                                  now=NOW, on_tool_start=lambda: events.append("tool_start"))
        self.assertEqual(result.action, "search")
        self.assertEqual(events, ["tool_start", "tool_request"])

        calls = []
        router = Router({"/chat/completions": plan_response({"action": "answer"})})
        result = await ground("写一首诗", [], PROVIDER, transport=router.transport(), now=NOW,
                              on_tool_start=lambda: calls.append(1))
        self.assertEqual(result.action, "answer")
        self.assertEqual(calls, [])

    async def test_on_tool_start_exception_is_swallowed(self):
        def boom():
            raise RuntimeError("callback broke")

        router = Router({
            "/chat/completions": plan_response({"action": "search", "query": "DeepSeek 最新模型"}),
            "/v1/tools/search": httpx.Response(200, json={"results": [SEARCH_RESULT]}),
        })
        with self.assertLogs("hub.web", level="INFO") as logs:
            result = await ground("DeepSeek 最近发布了什么", [], PROVIDER, transport=router.transport(),
                                  now=NOW, on_tool_start=boom)
        self.assertEqual(result.action, "search")
        self.assertTrue(any("on_tool_start callback failed" in line for line in logs.output))

    async def test_search_skips_private_and_incomplete_results(self):
        results = [
            {"title": "内网", "url": "http://192.168.1.1/x", "summary": "x"},
            {"title": "无摘要", "url": "https://a.example.com/", "summary": ""},
            {"title": "片段", "url": "https://b.example.com/p", "snippet": "片段内容"},
        ]
        router = Router({
            "/chat/completions": plan_response({"action": "search", "query": "q"}),
            "/v1/tools/search": httpx.Response(200, json={"results": results}),
        })
        with self.assertLogs("hub.web", level="INFO"):
            result = await ground("q", [], PROVIDER, transport=router.transport(), now=NOW)
        self.assertEqual(result.action, "search")
        self.assertEqual(result.sources, ["https://b.example.com/p"])
        self.assertIn("片段内容", result.evidence_messages[1]["content"])

    async def test_base_url_with_v1_derives_tool_url(self):
        provider = dict(PROVIDER, base_url="https://api.deepseek.com/v1/")
        router = Router({
            "/v1/chat/completions": plan_response({"action": "search", "query": "q"}),
            "/v1/tools/search": httpx.Response(200, json={"results": [SEARCH_RESULT]}),
        })
        with self.assertLogs("hub.web", level="INFO"):
            result = await ground("q", [], provider, transport=router.transport(), now=NOW)
        self.assertEqual(result.action, "search")
        self.assertEqual(router.paths(), ["/v1/chat/completions", "/v1/tools/search"])

    async def test_fetch_rejects_url_not_in_history(self):
        router = Router({
            "/chat/completions": plan_response({"action": "fetch", "url": "https://evil.example.com/page"}),
            "/v1/tools/fetch": httpx.Response(200, json={"title": "t", "content": "c"}),
        })
        history = [{"role": "user", "content": "看看 https://good.example.com/a"}]
        with self.assertLogs("hub.web", level="INFO"):
            result = await ground("读一下那个网页", history, PROVIDER, transport=router.transport(), now=NOW)
        self.assertEqual(result, WebResult(action="off", evidence_messages=[], sources=[]))
        self.assertNotIn("/v1/tools/fetch", router.paths())

    async def test_fetch_url_from_history(self):
        url = "https://good.example.com/a"
        router = Router({
            "/chat/completions": plan_response({"action": "fetch", "url": url}),
            "/v1/tools/fetch": httpx.Response(200, json={"title": "好文章", "content": "正文内容"}),
        })
        history = [{"role": "user", "content": f"看看 {url}"}, {"role": "assistant", "content": "好的"}]
        with self.assertLogs("hub.web", level="INFO"):
            result = await ground("读一下那个网页", history, PROVIDER, transport=router.transport(), now=NOW)
        self.assertEqual(result.action, "fetch")
        self.assertEqual(result.sources, [url])
        self.assertEqual(json.loads(router.calls[1].content), {"url": url})
        content = result.evidence_messages[1]["content"]
        self.assertEqual(content, "外部参考资料：\n" + json.dumps(
            {"title": "好文章", "url": url, "text": "正文内容"}, ensure_ascii=False))

    async def test_tool_failure_degrades_to_off(self):
        router = Router({
            "/chat/completions": plan_response({"action": "search", "query": "q"}),
            "/v1/tools/search": httpx.Response(500, json={"error": "boom"}),
        })
        with mock.patch.object(web, "RETRY_BACKOFF_MS", 0), self.assertLogs("hub.web", level="INFO") as logs:
            result = await ground("q", [], PROVIDER, transport=router.transport(), now=NOW)
        self.assertEqual(result, WebResult(action="off", evidence_messages=[], sources=[]))
        # One retry for retryable status (LLM_WEB_RETRIES=1).
        self.assertEqual(router.paths(), ["/chat/completions", "/v1/tools/search", "/v1/tools/search"])
        self.assertNotIn("sk-secret-key", "\n".join(logs.output))

    async def test_auth_error_not_retried(self):
        router = Router({"/chat/completions": httpx.Response(401, json={"error": "bad key"})})
        with self.assertLogs("hub.web", level="INFO"):
            result = await ground("q", [], PROVIDER, transport=router.transport(), now=NOW)
        self.assertEqual(result.action, "off")
        self.assertEqual(router.paths(), ["/chat/completions"])

    async def test_bad_plan_degrades_to_off(self):
        for resp in (
            plan_response({"action": "browse"}),
            plan_response({"action": "answer"}, finish_reason="length"),
            httpx.Response(200, text="not json"),
            httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{oops"}}]}),
        ):
            router = Router({"/chat/completions": resp})
            with self.assertLogs("hub.web", level="INFO"):
                result = await ground("q", [], PROVIDER, transport=router.transport(), now=NOW)
            self.assertEqual(result.action, "off")

    async def test_empty_search_results_degrade_to_off(self):
        router = Router({
            "/chat/completions": plan_response({"action": "search", "query": "q"}),
            "/v1/tools/search": httpx.Response(200, json={"results": []}),
        })
        with self.assertLogs("hub.web", level="INFO"):
            result = await ground("q", [], PROVIDER, transport=router.transport(), now=NOW)
        self.assertEqual(result.action, "off")

    async def test_timeout_degrades_to_off(self):
        def boom(request):
            raise httpx.ReadTimeout("timed out", request=request)

        router = Router({"/chat/completions": boom})
        with mock.patch.object(web, "RETRY_BACKOFF_MS", 0), self.assertLogs("hub.web", level="INFO"):
            result = await ground("q", [], PROVIDER, transport=router.transport(), now=NOW)
        self.assertEqual(result.action, "off")
        self.assertEqual(router.paths(), ["/chat/completions", "/chat/completions"])

    async def test_budget_exhausted_degrades_to_off(self):
        router = Router({"/chat/completions": plan_response({"action": "answer"})})
        with mock.patch.object(web, "TOTAL_BUDGET_S", 0), self.assertLogs("hub.web", level="INFO"):
            result = await ground("q", [], PROVIDER, transport=router.transport(), now=NOW)
        self.assertEqual(result.action, "off")
        self.assertEqual(router.calls, [])


class PublicUrlTest(unittest.TestCase):
    def test_public_url_rules(self):
        ok = ["https://example.com", "http://a.b.example.org/path?x=1#y"]
        bad = ["ftp://example.com", "https://localhost/", "https://10.0.0.1/", "https://user@example.com/",
               "https://example.com:8080/", "https://printer.local/", "https://[::1]/", "https://intranet/",
               "https://example.com/a b", "https://example.com/\"x\"", "https://x.example.com./"]
        for u in ok:
            self.assertTrue(web.is_public_url(u), u)
        for u in bad:
            self.assertFalse(web.is_public_url(u), u)


if __name__ == "__main__":
    unittest.main()
