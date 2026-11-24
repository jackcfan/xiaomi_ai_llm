import asyncio
import json
import os
import tempfile
import time
import unittest

import httpx

from hub.decision import SYSTEMONE_PATH, DecisionClient, choice_of, decide
from hub.store import Store


def _resp(status=200, body=None):
    body = json.dumps(body).encode("utf-8") if isinstance(body, (dict, list)) else body
    return httpx.Response(status, content=body or b"{}", request=httpx.Request("POST", "http://x"))


class ChoiceOfTest(unittest.TestCase):
    def test_parses_answers(self):
        self.assertEqual(choice_of({"intent": {"type": "choice", "choice": "qa", "confidence": 0.91,
                                               "probabilities": {"qa": 0.91}}}, "intent"),
                         ("qa", 0.91, {"qa": 0.91}))

    def test_missing_probabilities_tolerated(self):
        self.assertEqual(choice_of({"intent": {"type": "choice", "choice": "qa", "confidence": 0.9}},
                                   "intent"), ("qa", 0.9, {}))

    def test_unknown_choice_shape_returns_none(self):
        self.assertIsNone(choice_of({}, "intent"))
        self.assertIsNone(choice_of({"intent": {"choice": 3}}, "intent"))
        self.assertIsNone(choice_of({"intent": {"type": "noul", "noul": 0.9}}, "intent"))


class DecisionClientTest(unittest.TestCase):
    def test_request_shape(self):
        captured = {}

        def handler(request: httpx.Request):
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content)
            return _resp(200, {"model": "decision-model-preview",
                               "answers": {"intent": {"type": "choice", "choice": "qa",
                                                      "confidence": 0.9, "probabilities": {"qa": 0.9}}}})

        client = DecisionClient("https://x.maas.aliyuncs.com", "sk-abc", "decision-model-preview",
                                transport=httpx.MockTransport(handler))
        answers = asyncio.run(client.ask("切换到毒舌", {"intent": {"type": "choice",
                                                                   "instructions": "i",
                                                                   "criteria": {"qa": "普通提问"}}}))
        self.assertEqual(captured["url"],
                         "https://x.maas.aliyuncs.com" + SYSTEMONE_PATH)
        self.assertEqual(captured["headers"]["authorization"], "sk-abc")  # 无 Bearer
        self.assertEqual(captured["body"]["model"], "decision-model-preview")
        self.assertEqual(captured["body"]["state"], {"content": "切换到毒舌"})
        self.assertEqual(set(captured["body"]["questions"]["intent"]["criteria"]), {"qa"})
        self.assertEqual(answers["intent"]["choice"], "qa")

    def test_strips_compatible_mode_prefix(self):
        # 供应商 base_url 可能已含 /compatible-mode/v1（百炼控制台复制来的），须剥掉再拼。
        captured = {}

        def handler(request: httpx.Request):
            captured["url"] = str(request.url)
            return _resp(200, {"answers": {}})

        client = DecisionClient(
            "https://x.maas.aliyuncs.com/compatible-mode/v1", "sk", "m",
            transport=httpx.MockTransport(handler))
        asyncio.run(client.ask("x", {}))
        self.assertEqual(captured["url"],
                         "https://x.maas.aliyuncs.com" + SYSTEMONE_PATH)

    def test_missing_answers_returns_none(self):
        client = DecisionClient("https://x", "sk", "m",
                                transport=httpx.MockTransport(lambda r: _resp(200, {"foo": 1})))
        self.assertIsNone(asyncio.run(client.ask("x", {})))

    def test_failures_return_none(self):
        def timeout_handler(request):
            raise httpx.ConnectTimeout("boom")

        for transport in (
            httpx.MockTransport(timeout_handler),
            httpx.MockTransport(lambda r: _resp(500, {"error": "down"})),
            httpx.MockTransport(lambda r: _resp(200, b"not json")),
        ):
            with self.subTest(transport=transport):
                client = DecisionClient("https://x", "sk", "m", transport=transport)
                self.assertIsNone(asyncio.run(client.ask("x", {})))

    def test_error_log_does_not_contain_key(self):
        client = DecisionClient("https://x", "sk-abc", "m",
                                transport=httpx.MockTransport(
                                    lambda r: _resp(500, {"error": "sk-abc leaked"})))
        with self.assertLogs("hub.decision", level="WARNING") as logs:
            asyncio.run(client.ask("x", {}))
        self.assertNotIn("sk-abc", "\n".join(logs.output))


class FakeDecideClient:
    """按顺序返回预置 answers；第二个答案（entity 阶段）用 target 字段。"""

    def __init__(self, answers, delay=0.0):
        self._answers = dict(answers)
        self.calls = 0
        self.delay = delay

    async def ask(self, content, questions):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        name = next(iter(questions))
        if name == "target":
            return {"target": self._answers.get("target", {})}
        return {"intent": self._answers.get("intent", {})}


class DecideTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "hub.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_stage1_qa_returns_none(self):
        client = FakeDecideClient({"intent": {"choice": "qa", "confidence": 0.95}})
        self.assertIsNone(asyncio.run(decide("今天天气怎么样", self.store, client, 0.8, 0.6)))
        self.assertEqual(client.calls, 1)  # 不追问实体

    def test_low_confidence_returns_none(self):
        client = FakeDecideClient({"intent": {"choice": "switch_target", "confidence": 0.4}})
        self.assertIsNone(asyncio.run(decide("切换到银月", self.store, client, 0.8, 0.6)))

    def test_stage2_only_for_entity_intent(self):
        client = FakeDecideClient({"intent": {"choice": "reset_context", "confidence": 0.9}})
        d = asyncio.run(decide("重新开始", self.store, client, 0.8, 0.6))
        self.assertEqual(client.calls, 1)
        self.assertEqual(d.intent_key, "reset_context")

    def test_target_must_appear_in_text(self):
        # 模型选了「温柔」，但原话里没有「温柔」→ 视为幻觉
        client = FakeDecideClient({"intent": {"choice": "switch_target", "confidence": 0.9},
                                   "target": {"choice": "温柔", "confidence": 0.9}})
        self.assertIsNone(asyncio.run(decide("切换到银月", self.store, client, 0.8, 0.6)))

    def test_returns_resolved_entity(self):
        client = FakeDecideClient({"intent": {"choice": "switch_target", "confidence": 0.9},
                                   "target": {"choice": "毒舌", "confidence": 0.9}})
        d = asyncio.run(decide("切换到毒舌吧", self.store, client, 0.8, 0.6))
        self.assertEqual(d.intent_key, "switch_target")
        self.assertEqual(d.entity["kind"], "persona")
        self.assertEqual(d.entity["name"], "毒舌")

    def test_budget_shared_across_stages(self):
        slow = FakeDecideClient({"intent": {"choice": "switch_target", "confidence": 0.9},
                                 "target": {"choice": "毒舌", "confidence": 0.9}}, delay=0.3)
        start = time.monotonic()
        d = asyncio.run(decide("切换到毒舌", self.store, slow, 0.4, 0.6))
        self.assertLess(time.monotonic() - start, 0.7)  # 两阶段合计不超过预算
        self.assertIsNone(d)  # 超时被截断

    def test_choice_outside_criteria_is_ignored(self):
        client = FakeDecideClient({"intent": {"choice": "不存在的意图", "confidence": 0.99}})
        self.assertIsNone(asyncio.run(decide("随便说说", self.store, client, 0.8, 0.6)))


if __name__ == "__main__":
    unittest.main()
