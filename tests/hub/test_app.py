import asyncio
import contextlib
import io
import os
import struct
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from hub.app import create_app
from hub.chat import APOLOGY_TEXT, FILLER_TEXT, ChatService
from hub.store import Store
from hub.web import WebResult

# 振幅 0x1000，高于 StreamingPipeline 尾部静音裁剪阈值（96），否则会被裁成空。
PCM = b"\x00\x10" * 160
TOKENS = ["第一句话。", "第二句话。", "第三句话。"]
FULL_ANSWER = "".join(TOKENS)


def wav(pcm: bytes, rate: int = 32000) -> bytes:
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ",
        16, 1, 1, rate, rate * 2, 2, 16,
        b"data", len(pcm),
    ) + pcm


class FakeLLM:
    def __init__(self, owner, kwargs):
        self.owner = owner
        self.kwargs = kwargs

    async def chat_stream(self, message, system_prompt=None, conversation_history=None):
        self.owner.calls.append({
            "message": message,
            "system_prompt": system_prompt,
            "conversation_history": list(conversation_history or []),
            "client": self.kwargs,
        })
        try:
            if self.owner.error is not None:
                raise self.owner.error
            for token in self.owner.tokens:
                yield token
            self.owner.finished = True
        finally:
            self.owner.closed = True


class FakeLLMFactory:
    def __init__(self, tokens=None, error=None):
        self.tokens = list(TOKENS if tokens is None else tokens)
        self.error = error
        self.calls = []
        self.closed = False
        self.finished = False

    def __call__(self, **kwargs):
        return FakeLLM(self, kwargs)


class FakeTTS:
    def __init__(self, owner, voice):
        self.owner = owner
        self.voice = voice

    async def synthesize_pcm(self, text, sample_rate=32000, speed=1.0, volume=1.0):
        self.owner.texts.append((self.voice, text))
        return wav(PCM, sample_rate)


class FakeTTSFactory:
    def __init__(self):
        self.voices = []
        self.texts = []

    def __call__(self, voice):
        self.voices.append(voice)
        return FakeTTS(self, voice)


class FakeGround:
    def __init__(self, result=None):
        self.result = result or WebResult("answer", [], [])
        self.calls = []

    async def __call__(self, question, history, provider, on_tool_start=None, **kwargs):
        self.calls.append((question, list(history), provider))
        if self.result.action in ("search", "fetch") and on_tool_start is not None:
            on_tool_start()  # 规划决定检索、工具请求之前
            if self.release is not None:
                await self.release.wait()  # 测试看到垫话 PCM 后才放行
        self.completed += 1
        return self.result

    release = None
    completed = 0


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class HubAppTest(unittest.TestCase):
    def setUp(self):
        self._stdout = contextlib.redirect_stdout(io.StringIO())
        self._stdout.__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "hub.db"))
        self.llm = FakeLLMFactory()
        self.tts = FakeTTSFactory()
        self.ground = FakeGround()
        self.clock = Clock()
        self.service = ChatService(
            self.store,
            llm_factory=self.llm,
            tts_factory=self.tts,
            web_ground=self.ground,
            clock=self.clock,
        )
        self.client = TestClient(create_app(self.store, self.service))

    def tearDown(self):
        self.client.close()
        self.store.close()
        self.tmp.cleanup()
        self._stdout.__exit__(None, None, None)

    def persona(self, name):
        return next(p for p in self.store.list_personas() if p["name"] == name)

    def ask(self, message):
        return self.client.post("/api/v1/stream/text_chat", data={
            "message": message, "session_id": "shell", "backend": "x", "speed": "1.0", "volume": "1.0",
        })

    def test_health(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_text_chat_streams_wav_and_saves_turn(self):
        resp = self.ask("世界首富是谁")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content[:4], b"RIFF")
        self.assertGreater(len(resp.content), 44)
        self.assertEqual(resp.headers.get("x-accel-buffering"), "no")
        turn = self.store.recent_turns(1)[0]
        self.assertEqual(turn["kind"], "qa")
        self.assertEqual(turn["answer"], FULL_ANSWER)
        self.assertEqual(turn["persona_name"], "默认")
        self.assertEqual(turn["provider_name"], "DeepSeek")
        self.assertEqual(turn["user_text"], "世界首富是谁")
        self.assertEqual(turn["interrupted"], 0)
        self.assertEqual(self.llm.calls[0]["message"], "世界首富是谁")

    def test_provider_extra_body_passed_to_llm(self):
        resp = self.ask("你好")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.llm.calls[0]["client"]["extra_body"], {"thinking": {"type": "disabled"}})
        pid = self.store.current_provider()["id"]
        for raw in (None, "", "{bad"):
            self.store.upsert_provider({"id": pid, "extra_body": raw})
            self.llm.calls.clear()
            if raw == "{bad":
                with self.assertLogs("hub.chat", "WARNING"):
                    self.ask("你好")
            else:
                self.ask("你好")
            self.assertIsNone(self.llm.calls[0]["client"]["extra_body"])

    def test_uses_persona_prompt_and_voice(self):
        dushe = self.persona("毒舌")
        self.store.set_setting("current_persona_id", str(dushe["id"]))
        resp = self.ask("讲个笑话")
        self.assertEqual(resp.status_code, 200)
        prompt = self.llm.calls[0]["system_prompt"]
        self.assertIn(dushe["prompt"], prompt)
        self.assertTrue(prompt.startswith(self.store.get_setting("base_prompt") + "\n"))
        self.assertEqual(self.tts.voices, ["zh-CN-YunxiNeural"])

    def test_command_switches_without_llm(self):
        resp = self.ask("换成温柔模式")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content[:4], b"RIFF")
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.store.current_persona()["name"], "温柔")
        voice = self.persona("温柔")["voice"]
        self.assertEqual(self.tts.texts, [(voice, "好的，已切换为温柔")])
        turn = self.store.recent_turns(1)[0]
        self.assertEqual(turn["kind"], "command")
        self.assertEqual(turn["persona_name"], "温柔")

    def test_provider_command_and_reset(self):
        kimi_id = self.store.upsert_provider({
            "name": "Kimi", "aliases": "kimi", "base_url": "https://api.moonshot.cn/v1",
            "api_key": "sk-k", "model": "kimi-k2",
        })
        self.assertEqual(self.ask("切换到Kimi模型").status_code, 200)
        self.assertEqual(self.store.get_setting("current_provider_id"), str(kimi_id))
        self.assertEqual(self.tts.texts[-1][1], "好的，已切换到Kimi")
        self.assertEqual(self.store.recent_turns(1)[0]["provider_name"], "Kimi")

        self.assertEqual(self.ask("重新开始").status_code, 200)
        self.assertEqual(self.tts.texts[-1][1], "好的，我们重新开始")
        self.assertEqual(self.store.recent_turns(1)[0]["kind"], "reset")
        self.assertEqual(self.llm.calls, [])

    def test_context_passed_within_gap(self):
        self.clock.now = 1000
        self.assertEqual(self.ask("问题A").status_code, 200)
        self.clock.now = 1100
        self.assertEqual(self.ask("问题B").status_code, 200)
        history = self.llm.calls[1]["conversation_history"]
        self.assertEqual(history, [
            {"role": "user", "content": "问题A"},
            {"role": "assistant", "content": FULL_ANSWER},
        ])
        self.assertEqual(self.ground.calls[1][1], history)
        self.clock.now = 2000
        self.assertEqual(self.ask("问题C").status_code, 200)
        self.assertEqual(self.llm.calls[2]["conversation_history"], [])

    def test_context_settings_parsed_with_fallback(self):
        self.store.set_setting("context_gap_seconds", "abc")
        self.store.set_setting("context_max_turns", "")
        self.clock.now = 1000
        self.ask("问题A")
        self.clock.now = 1200
        self.ask("问题B")
        self.assertEqual(len(self.llm.calls[1]["conversation_history"]), 2)

    def test_empty_context_messages_dropped(self):
        self.store.add_turn("被打断的问题", "", "DeepSeek", "默认", "qa", interrupted=True, ts=990)
        self.clock.now = 1000
        self.ask("问题B")
        self.assertEqual(self.llm.calls[0]["conversation_history"],
                         [{"role": "user", "content": "被打断的问题"}])

    def test_search_plays_filler_first(self):
        evidence = [
            {"role": "system", "content": "证据系统提示"},
            {"role": "user", "content": "外部参考资料：\n{...}"},
        ]
        self.ground.result = WebResult("search", evidence, ["https://example.com/a"])
        self.clock.now = 1000
        self.ask("问题A")
        self.clock.now = 1100
        resp = self.ask("今天金价多少")
        self.assertEqual(resp.status_code, 200)
        texts = [t for _, t in self.tts.texts]
        self.assertEqual(texts[0], FILLER_TEXT)
        self.assertEqual(FILLER_TEXT, "搜索中，请稍等。")
        # 第二次搜索复用缓存的垫话 PCM，不再合成。
        self.assertEqual(texts.count(FILLER_TEXT), 1)
        self.assertEqual(len(resp.content), 44 + len(PCM) * (len(TOKENS) + 1))
        call = self.llm.calls[1]
        # 证据紧跟问题：…context, user(问题), system(证据提示), user(外部参考资料) 最后。
        self.assertEqual(call["conversation_history"][-2:], [
            {"role": "user", "content": "今天金价多少"},
            evidence[0],
        ])
        self.assertEqual(call["conversation_history"][:2], [
            {"role": "user", "content": "问题A"},
            {"role": "assistant", "content": FULL_ANSWER},
        ])
        self.assertEqual(call["message"], evidence[1]["content"])
        turn = self.store.recent_turns(1)[0]
        self.assertEqual(turn["user_text"], "今天金价多少")
        self.assertEqual(turn["web_sources"], "https://example.com/a")

    def test_filler_yielded_before_ground_completes(self):
        evidence = [
            {"role": "system", "content": "证据系统提示"},
            {"role": "user", "content": "外部参考资料：\n{...}"},
        ]
        self.ground.result = WebResult("search", evidence, ["https://example.com/a"])

        async def run():
            self.ground.release = asyncio.Event()
            gen = self.service.answer("今天金价多少", 1.0, 1.0)
            first = await gen.__anext__()
            # 检索尚未完成、模型尚未调用，垫话已经产出。
            self.assertEqual(first, PCM)
            self.assertEqual(self.ground.completed, 0)
            self.assertEqual([t for _, t in self.tts.texts], [FILLER_TEXT])
            self.assertEqual(self.llm.calls, [])
            self.ground.release.set()
            rest = [pcm async for pcm in gen]
            self.assertEqual(len(rest), len(TOKENS))
            self.assertEqual(self.ground.completed, 1)

        asyncio.run(run())
        self.assertEqual(self.llm.calls[0]["message"], evidence[1]["content"])
        turn = self.store.recent_turns(1)[0]
        self.assertEqual((turn["answer"], turn["interrupted"]), (FULL_ANSWER, 0))

    def test_failure_after_filler_apologizes_and_saves(self):
        self.ground.result = WebResult(
            "search", [{"role": "system", "content": "s"}, {"role": "user", "content": "外部参考资料：x"}],
            ["https://example.com/a"])
        for case in ("error", "empty"):
            with self.subTest(case=case):
                self.llm.error = RuntimeError("boom") if case == "error" else None
                self.llm.tokens = [] if case == "empty" else list(TOKENS)
                self.tts.texts.clear()
                with self.assertLogs("hub.chat", "WARNING"):
                    resp = self.ask("今天金价多少")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual([t for _, t in self.tts.texts][-1], APOLOGY_TEXT)
                self.assertEqual(APOLOGY_TEXT, "抱歉，暂时无法回答。")
                turn = self.store.recent_turns(1)[0]
                self.assertEqual((turn["answer"], turn["interrupted"], turn["kind"]), ("", 1, "qa"))
                self.assertEqual(turn["web_sources"], "https://example.com/a")
        self.assertEqual(len(self.store.recent_turns(5)), 2)

    def test_filler_tts_failure_is_skipped(self):
        self.ground.result = WebResult(
            "search", [{"role": "system", "content": "s"}, {"role": "user", "content": "外部参考资料：x"}], [])
        tts = self.tts

        class FillerFailTTS(FakeTTS):
            async def synthesize_pcm(inner, text, sample_rate=32000, speed=1.0, volume=1.0):
                if text == FILLER_TEXT:
                    raise RuntimeError("tts down")
                return await super().synthesize_pcm(text, sample_rate, speed, volume)

        self.service.tts_factory = lambda voice: FillerFailTTS(tts, voice)
        with self.assertLogs("hub.chat", "WARNING") as logs:
            resp = self.ask("今天金价多少")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.content), 44 + len(PCM) * len(TOKENS))
        self.assertTrue(any("filler TTS failed" in line for line in logs.output))
        turn = self.store.recent_turns(1)[0]
        self.assertEqual((turn["answer"], turn["interrupted"]), (FULL_ANSWER, 0))

    def test_model_error_before_audio_returns_502(self):
        self.llm.error = RuntimeError("boom")
        with self.assertLogs("hub.app", "WARNING"):
            resp = self.ask("世界首富是谁")
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(self.store.recent_turns(5), [])

    def test_empty_answer_returns_502(self):
        self.llm.tokens = []
        with self.assertLogs("hub.app", "WARNING"):
            resp = self.ask("世界首富是谁")
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(self.store.recent_turns(5), [])

    def test_no_provider_returns_502(self):
        for p in self.store.list_providers():
            self.store.delete_provider(p["id"])
        with self.assertLogs("hub", "WARNING"):
            resp = self.ask("世界首富是谁")
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.store.recent_turns(5), [])

    def test_disconnect_saves_partial_interrupted(self):
        async def run():
            gen = self.service.answer("讲个故事", 1.0, 1.0)
            first = await gen.__anext__()
            self.assertTrue(first)
            await gen.aclose()
            # 关闭后立即（不依赖 GC/终结器）落库并关闭模型流。
            turns = self.store.recent_turns(1)
            self.assertEqual(len(turns), 1)
            self.assertEqual(turns[0]["interrupted"], 1)
            self.assertTrue(self.llm.closed)

        asyncio.run(run())
        turn = self.store.recent_turns(1)[0]
        self.assertEqual(turn["interrupted"], 1)
        self.assertEqual(turn["kind"], "qa")
        self.assertTrue(turn["answer"])
        self.assertTrue(FULL_ANSWER.startswith(turn["answer"]))
        self.assertNotEqual(turn["answer"], FULL_ANSWER)
        self.assertTrue(self.llm.closed)
        self.assertFalse(self.llm.finished)

    def test_cancel_mid_tts_saves_partial_interrupted(self):
        # 断开在 await 中以 CancelledError 送达（Starlette 取消任务）时同样保存并关模型流。
        started = []

        class BlockingTTS(FakeTTS):
            async def synthesize_pcm(inner, text, sample_rate=32000, speed=1.0, volume=1.0):
                if started:
                    await asyncio.Event().wait()  # 第二句永远阻塞
                started.append(text)
                return wav(PCM, sample_rate)

        self.service.tts_factory = lambda voice: BlockingTTS(self.tts, voice)

        async def run():
            got = []

            async def consume():
                async for pcm in self.service.answer("讲个故事", 1.0, 1.0):
                    got.append(pcm)

            task = asyncio.create_task(consume())
            while not got:
                await asyncio.sleep(0)
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        turn = self.store.recent_turns(1)[0]
        self.assertEqual(turn["interrupted"], 1)
        self.assertTrue(FULL_ANSWER.startswith(turn["answer"]))
        self.assertTrue(turn["answer"])
        self.assertTrue(self.llm.closed)
        self.assertFalse(self.llm.finished)

    def test_tts_stream_endpoint(self):
        resp = self.client.post("/api/v1/tts/stream", data={"message": "你好", "speed": "1.0", "volume": "1.0"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content[:4], b"RIFF")
        self.assertGreater(len(resp.content), 44)
        self.assertEqual(self.tts.voices, [self.store.current_persona()["voice"]])
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.store.recent_turns(5), [])

    def test_siliconflow_persona_uses_provider_key(self):
        # 当前人设切到 siliconflow：ChatService 应从 providers 里按名取 key 传给工厂，
        # 而不是走默认 EdgeTTS 工厂。
        import hub.chat as chat_mod

        self.store.upsert_provider({
            "name": "硅基流动", "aliases": "siliconflow", "base_url": "https://api.siliconflow.cn/v1",
            "api_key": "sk-sf-9999", "model": "deepseek-flash",
        })
        pid = self.store.upsert_persona({
            "name": "cosy", "aliases": "", "prompt": "p",
            "voice": "speech:sqz:test:voice", "tts_provider": "siliconflow",
        })
        self.store.set_setting("current_persona_id", str(pid))
        seen = {}
        with mock.patch.object(chat_mod, "siliconflow_tts_factory",
                               side_effect=lambda voice, api_key, base_url="": seen.update(
                                   voice=voice, api_key=api_key, base_url=base_url)):
            self.service._make_tts(self.store.current_persona())
        self.assertEqual(seen["voice"], "speech:sqz:test:voice")
        self.assertEqual(seen["api_key"], "sk-sf-9999")
        self.assertEqual(seen["base_url"], "https://api.siliconflow.cn/v1")

    def test_siliconflow_persona_without_provider_raises(self):
        # providers 里没有 siliconflow 行时，明确报错，绝不拿其它厂商的 key 发往硅基流动。
        from hub.chat import ChatError
        pid = self.store.upsert_persona({
            "name": "cosy", "aliases": "", "prompt": "p",
            "voice": "speech:sqz:test:voice", "tts_provider": "siliconflow",
        })
        self.store.set_setting("current_persona_id", str(pid))
        with self.assertRaises(ChatError):
            self.service._make_tts(self.store.current_persona())


if __name__ == "__main__":
    unittest.main()
