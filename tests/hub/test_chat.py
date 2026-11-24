import asyncio
import contextlib
import io
import os
import struct
import tempfile
import time
import unittest

from hub.chat import ChatService
from hub.decision import Decision
from hub.intents import QA_LABEL
from hub.rules import normalize
from hub.store import Store

PCM = b"\x00\x10" * 160


def wav(pcm: bytes, rate: int = 32000) -> bytes:
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ",
        16, 1, 1, rate, rate * 2, 2, 16,
        b"data", len(pcm),
    ) + pcm


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


class FailIfCalledLLM:
    def __init__(self, owner, kwargs):
        self.owner = owner

    async def chat_stream(self, message, system_prompt=None, conversation_history=None):
        raise AssertionError("LLM should not be called on the rule path")


class FailIfCalledFactory:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return FailIfCalledLLM(self, kwargs)


def collect(pcm_iter):
    async def run():
        out = []
        async for chunk in pcm_iter:
            out.append(chunk)
        return out

    return asyncio.run(run())


class ChatRouteTest(unittest.TestCase):
    def setUp(self):
        self._stdout = contextlib.redirect_stdout(io.StringIO())
        self._stdout.__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "hub.db"))
        self.llm = FailIfCalledFactory()
        self.tts = FakeTTSFactory()
        self.service = ChatService(
            self.store,
            llm_factory=self.llm,
            tts_factory=self.tts,
            web_ground=None,
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        self._stdout.__exit__(None, None, None)

    def test_rule_path_executes_without_qa(self):
        chunks = collect(self.service.answer("切换到毒舌", 1.0, 1.0))
        self.assertTrue(chunks)
        self.assertEqual(self.store.current_persona()["name"], "毒舌")
        turn = self.store.list_turns(0, 1)[0]
        self.assertEqual((turn["route"], turn["intent_key"], turn["learn_state"]),
                         ("rule", "switch_target", "new"))
        self.assertIsNotNone(turn["rule_id"])

    def test_rule_path_bumps_rule_stats(self):
        collect(self.service.answer("换成温柔模式", 1.0, 1.0))
        rule = next(r for r in self.store.list_rules() if r["intent_key"] == "switch_target")
        self.assertEqual(rule["hit_count"], 1)
        self.assertIsNotNone(rule["last_hit_ts"])

    def test_reset_rule_path(self):
        collect(self.service.answer("忘掉刚才的对话", 1.0, 1.0))
        turn = self.store.list_turns(0, 1)[0]
        self.assertEqual((turn["route"], turn["intent_key"], turn["kind"]),
                         ("rule", "reset_context", "reset"))

    def test_unmatched_text_goes_to_qa(self):
        # 无 LLM 时 _qa 会失败（首块 PCM 前抛错）——这里只验证路由决策本身，
        # 用 _route 直接断言更干净。
        route = asyncio.run(self.service._route("今天天气怎么样"))
        self.assertEqual((route.route, route.intent_key), ("none", ""))

    def test_question_form_not_matched_by_rule_layer(self):
        # 「换成毒舌吗」的「吗」被收进 target，正则层解析不到实体 → 未命中。
        # 该句式的执行在决策模型层（Task 7）完成，这里钉住正则层不误执行。
        route = asyncio.run(self.service._route("换成毒舌吗"))
        self.assertEqual(route.route, "none")
        self.assertEqual(self.store.current_persona()["name"], "默认")


class DecisionRouteTest(unittest.TestCase):
    def setUp(self):
        self._stdout = contextlib.redirect_stdout(io.StringIO())
        self._stdout.__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "hub.db"))
        self.tts = FakeTTSFactory()
        # 决策路由需要一个 decision provider（_route 据此构造 DecisionClient 传给 decide）。
        self.store.upsert_provider({
            "name": "百炼决策", "aliases": "", "base_url": "https://x.maas.aliyuncs.com",
            "api_key": "sk-dec", "model": "decision-model-preview",
        })
        self.store.set_setting("decision_provider_id", str(self.store.list_providers()[-1]["id"]))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        self._stdout.__exit__(None, None, None)

    def make_service(self, decision):
        return ChatService(
            self.store,
            llm_factory=FailIfCalledFactory(),
            tts_factory=self.tts,
            web_ground=None,
            decision=decision,
        )

    def test_model_path_executes_command(self):
        pid = self.store.get_persona(3)["id"]

        async def decision(text, store, client, budget_s, min_conf, out=None):
            return Decision("switch_target", confidence=0.9,
                            entity={"kind": "persona", "id": pid, "name": "毒舌"})

        service = self.make_service(decision)
        chunks = collect(service.answer("换成那个会吐槽的", 1.0, 1.0))
        self.assertTrue(chunks)
        self.assertEqual(self.store.current_persona()["name"], "毒舌")
        turn = self.store.list_turns(0, 1)[0]
        self.assertEqual((turn["route"], turn["intent_key"], turn["learn_state"]),
                         ("model", "switch_target", "new"))
        self.assertIn("0.9", turn["decision"])

    def test_decision_said_qa_records_qa_intent(self):
        async def decision(text, store, client, budget_s, min_conf, out=None):
            return None  # decide() 判成 qa 时返回 None

        service = self.make_service(decision)
        route = asyncio.run(service._route("今天天气怎么样"))
        self.assertEqual(route.route, "model")
        self.assertEqual(route.intent_key, QA_LABEL)

    def test_qa_path_carries_raw_decision_for_diagnostics(self):
        # 判成 qa 时，阶段一原始判定要随 route.decision 带出，落库后便于查「为什么判成 qa」。
        async def decision(text, store, client, budget_s, min_conf, out=None):
            out.intent = {"choice": "qa", "confidence": 0.95,
                          "probabilities": {"qa": 0.95}}
            return None

        service = self.make_service(decision)
        route = asyncio.run(service._route("切换到妈妈模式"))
        self.assertEqual((route.route, route.intent_key), ("model", "qa"))
        self.assertEqual(route.decision["choice"], "qa")
        self.assertEqual(route.decision["confidence"], 0.95)

    def test_gate_len_skips_decision_call(self):
        calls = []

        async def spy_decision(text, store, client, budget_s, min_conf, out=None):
            calls.append(text)
            return None

        service = self.make_service(spy_decision)
        text = "请帮我详细讲一下这个问题的来龙去脉以及我们应该怎么处理它才好呢顺便也说说有什么风险"
        self.assertGreater(len(normalize(text)), 40)
        route = asyncio.run(service._route(text))
        self.assertEqual(calls, [])
        self.assertEqual(route.route, "none")

    def test_decision_disabled_skips_decision_call(self):
        self.store.set_setting("decision_enabled", "0")
        calls = []

        async def spy_decision(text, store, client, budget_s, min_conf, out=None):
            calls.append(text)
            return None

        service = self.make_service(spy_decision)
        route = asyncio.run(service._route("换成那个会吐槽的"))
        self.assertEqual(calls, [])
        self.assertEqual(route.route, "none")

    def test_decision_exception_falls_back_none(self):
        async def raising_decision(text, store, client, budget_s, min_conf, out=None):
            raise TimeoutError("boom")

        service = self.make_service(raising_decision)
        route = asyncio.run(service._route("换成那个会吐槽的"))
        self.assertEqual(route.route, "none")


class ConfirmAndRecheckTest(unittest.TestCase):
    def setUp(self):
        self._stdout = contextlib.redirect_stdout(io.StringIO())
        self._stdout.__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "hub.db"))
        self.tts = FakeTTSFactory()
        self.store.upsert_provider({
            "name": "百炼决策", "aliases": "", "base_url": "https://x.maas.aliyuncs.com",
            "api_key": "sk-dec", "model": "decision-model-preview",
        })
        self.store.set_setting("decision_provider_id", str(self.store.list_providers()[-1]["id"]))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        self._stdout.__exit__(None, None, None)

    def make_service(self, decision=None, clock=None):
        return ChatService(
            self.store,
            llm_factory=FailIfCalledFactory(),
            tts_factory=self.tts,
            web_ground=None,
            decision=decision,
            clock=clock or time.time,
        )

    def test_confirm_band_asks_without_executing(self):
        pid = self.store.get_persona(3)["id"]

        async def decision(text, store, client, budget_s, min_conf, out=None):
            return Decision("switch_target", confidence=0.7,
                            entity={"kind": "persona", "id": pid, "name": "毒舌"})

        service = self.make_service(decision)
        chunks = collect(service.answer("换成那个会吐槽的", 1.0, 1.0))
        self.assertTrue(chunks)
        self.assertNotEqual(self.store.current_persona()["name"], "毒舌")
        turn = self.store.list_turns(0, 1)[0]
        self.assertEqual(turn["answer"], "你是要切换到毒舌吗")
        self.assertEqual(turn["intent_key"], "switch_target")

    def test_confirm_followup_executes(self):
        pid = self.store.get_persona(3)["id"]

        async def decision(text, store, client, budget_s, min_conf, out=None):
            return Decision("switch_target", confidence=0.7,
                            entity={"kind": "persona", "id": pid, "name": "毒舌"})

        service = self.make_service(decision)
        collect(service.answer("换成那个会吐槽的", 1.0, 1.0))
        collect(service.answer("对", 1.0, 1.0))
        self.assertEqual(self.store.current_persona()["name"], "毒舌")

    def test_confirm_expires_after_30s(self):
        pid = self.store.get_persona(3)["id"]

        async def decision(text, store, client, budget_s, min_conf, out=None):
            return Decision("switch_target", confidence=0.7,
                            entity={"kind": "persona", "id": pid, "name": "毒舌"})

        now = [1000.0]

        def clock():
            return now[0]

        service = self.make_service(decision, clock=clock)
        collect(service.answer("换成那个会吐槽的", 1.0, 1.0))
        now[0] = 1031.0  # 31 秒后
        collect(service.answer("对", 1.0, 1.0))
        self.assertNotEqual(self.store.current_persona()["name"], "毒舌")

    def test_recheck_mismatch_increments_counters(self):
        async def decision(text, store, client, budget_s, min_conf, out=None):
            return None  # 复核时模型判成 qa → 不一致

        service = self.make_service(decision)
        self.store.set_setting("decision_recheck_ratio", "1.0")
        rule_id = next(r["id"] for r in self.store.list_rules()
                       if r["intent_key"] == "switch_target")
        route = type("R", (), {"route": "rule", "intent_key": "switch_target",
                               "rule_id": rule_id})()

        async def run():
            task = service._maybe_recheck("切换到毒舌", route)
            await asyncio.wait_for(task, timeout=5)

        asyncio.run(run())
        row = self.store.get_rule(rule_id)
        self.assertEqual((row["recheck_count"], row["mismatch_count"]), (1, 1))

    def test_correction_marks_previous_turn(self):
        # 直接验证纠正检测状态机：上一条命令已执行，下一条纠正短语把该 turn 标记 corrected。
        now = [1000.0]
        service = self.make_service(clock=lambda: now[0])
        audio, turn_id = asyncio.run(self._run_first_command(service))
        # _run_intent 是 async 函数返回 (audio, turn_id)；这里不消费 audio。
        service._last_command = (1000.0, "switch_target", "毒舌", turn_id)
        service._maybe_mark_correction("不对，是温柔")
        self.assertEqual(self.store.list_turns(0, 1)[0]["corrected"], 1)

    def test_correction_fires_before_routing(self):
        # 回归审查发现：_maybe_mark_correction 原本只挂在 qa 分支，命令后纠正永远不触发。
        # 现在 answer() 在路由前先做纠正检测。这里用 mock 验证调用顺序。
        from unittest import mock
        from hub.chat import Route

        async def empty_qa(*args, **kwargs):
            if False:
                yield b""

        async def fake_route(text):
            order.append("route")
            return Route()

        now = [1000.0]
        service = self.make_service(clock=lambda: now[0])
        order = []
        service._last_command = (1000.0, "switch_target", "毒舌", 999)
        with mock.patch.object(service, "_maybe_mark_correction",
                               side_effect=lambda norm: order.append("correction")) as mc, \
             mock.patch.object(service, "_route", side_effect=fake_route) as mr, \
             mock.patch.object(service, "_qa", side_effect=empty_qa):
            collect(service.answer("切换到毒舌", 1.0, 1.0))
        self.assertEqual(order, ["correction", "route"])
        mc.assert_called_once_with("切换到毒舌")

    async def _run_first_command(self, service):
        # 直接触发一条 rule 路径命令，拿到落库的 turn_id。
        route = await service._route("切换到毒舌")
        audio, turn_id = await service._run_intent(route, "切换到毒舌", 1.0, 1.0)
        return audio, turn_id


class AutoDisableTest(unittest.TestCase):
    def setUp(self):
        self._stdout = contextlib.redirect_stdout(io.StringIO())
        self._stdout.__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "hub.db"))
        self.tts = FakeTTSFactory()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        self._stdout.__exit__(None, None, None)

    def test_auto_disable_after_mismatch_ratio(self):
        rid = next(r["id"] for r in self.store.list_rules()
                   if r["intent_key"] == "switch_target")
        for _ in range(10):
            self.store.bump_rule_stats(rid, recheck=True, mismatch=True)
        row = self.store.get_rule(rid)
        self.assertEqual((row["recheck_count"], row["mismatch_count"]), (10, 10))
        self.assertGreaterEqual(row["mismatch_count"] / row["recheck_count"], 0.25)

        from hub.chat import ChatService
        service = ChatService(self.store, tts_factory=self.tts, web_ground=None)
        disabled = service._maybe_disable_rule(rid)
        self.assertTrue(disabled)
        self.assertEqual(self.store.get_rule(rid)["status"], "disabled")


if __name__ == "__main__":
    unittest.main()
