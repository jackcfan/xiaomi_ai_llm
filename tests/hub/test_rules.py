import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from hub.chat import ChatService
from hub.rules import RuleSet, normalize, resolve_entity, load_ruleset
from hub.store import Store


def make_rule(id, intent_key, pattern, max_text_len=40, source="manual",
              status="active", priority=100):
    return {"id": id, "intent_key": intent_key, "pattern": pattern,
            "max_text_len": max_text_len, "source": source, "status": status,
            "priority": priority}


class NormalizeTest(unittest.TestCase):
    def test_normalize_strips_wake_prefix_and_punct(self):
        self.assertEqual(normalize("小爱同学，切换到毒舌模式。"), "切换到毒舌模式")
        self.assertEqual(normalize("  换一个话题  "), "换一个话题")
        self.assertEqual(normalize(""), "")


class RuleSetTest(unittest.TestCase):
    def test_length_gate_blocks_long_text(self):
        rs = RuleSet([make_rule(1, "switch_target", r"用(?P<target>.+)", max_text_len=4)],
                     resolve=lambda t: {"kind": "persona", "id": 3, "name": "毒舌"})
        self.assertIsNone(rs.match("用一下这个功能吧"))  # 8 字 > 4
        self.assertIsNotNone(rs.match("用毒舌"))

    def test_unresolvable_target_falls_through(self):
        rs = RuleSet(
            [
                make_rule(1, "switch_target", r"换成(?P<target>.+)", max_text_len=10, priority=100),
                make_rule(2, "reset_context", r"换成新的", max_text_len=10, priority=50),
            ],
            entity_intents={"switch_target"},
            resolve=lambda t: None,
        )
        # 规则 1 的 target「新的」解析不到实体 → 继续尝试规则 2
        self.assertEqual(rs.match("换成新的").rule.id, 2)

    def test_entity_intent_without_target_group_is_skipped(self):
        rs = RuleSet(
            [make_rule(1, "switch_target", r"切换到毒舌", max_text_len=10)],
            entity_intents={"switch_target"},
            resolve=lambda t: {"kind": "persona", "id": 3, "name": "毒舌"},
        )
        self.assertIsNone(rs.match("切换到毒舌"))

    def test_empty_norm_never_matches(self):
        rs = RuleSet([make_rule(1, "reset_context", r".*", max_text_len=10)])
        self.assertIsNone(rs.match(""))
        self.assertIsNone(rs.match(None))

    def test_priority_order(self):
        rs = RuleSet(
            [
                make_rule(2, "reset_context", r"重新开始|重来", priority=100),
                make_rule(1, "reset_context", r"重新开始", priority=50),
            ]
        )
        self.assertEqual(rs.match("重新开始").rule.id, 2)
    def test_bad_pattern_skipped(self):
        rs = RuleSet(
            [
                make_rule(1, "reset_context", r"([", priority=100),
                make_rule(2, "reset_context", r"重新开始", priority=50),
            ]
        )
        self.assertEqual(rs.match("重新开始").rule.id, 2)


class EntityAndLoadTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "hub.db")
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()

    def open_store(self):
        store = Store(self.path)
        self.stores.append(store)
        return store

    def test_resolve_entity_persona_then_provider_casefold(self):
        store = self.open_store()
        self.assertEqual(resolve_entity(store, "毒舌"),
                         {"kind": "persona", "id": 3, "name": "毒舌"})
        deepseek = resolve_entity(store, "DEEPSEEK")
        self.assertEqual(deepseek["kind"], "provider")
        self.assertEqual(deepseek["name"], "DeepSeek")
        self.assertIsNone(resolve_entity(store, "海盗"))
        self.assertIsNone(resolve_entity(store, ""))

    def test_manual_rules_seeded(self):
        store = self.open_store()
        rs = load_ruleset(store)
        self.assertEqual(rs.match("忘掉刚才的对话").rule.intent_key, "reset_context")
        self.assertEqual(rs.match("切换到毒舌").rule.intent_key, "switch_target")

    def test_switch_persona_and_provider(self):
        store = self.open_store()
        rs = load_ruleset(store)
        hit = rs.match("换成毒舌模式")
        self.assertEqual(hit.rule.intent_key, "switch_target")
        self.assertEqual(hit.entity, {"kind": "persona", "id": 3, "name": "毒舌"})
        hit = rs.match(normalize("小爱同学，切换到温柔"))
        self.assertEqual(hit.entity["name"], "温柔")
        hit = rs.match(normalize("换成DEEPSEEK模型。"))
        self.assertEqual(hit.entity, {"kind": "provider", "id": 1, "name": "DeepSeek"})

    def test_reset_phrases(self):
        store = self.open_store()
        rs = load_ruleset(store)
        for text in ("忘掉刚才的对话", "换个话题", "重新开始", "小爱同学，忘记刚才的对话。"):
            with self.subTest(text=text):
                self.assertEqual(rs.match(normalize(text)).rule.intent_key, "reset_context")

    def test_question_mentioning_persona_is_not_rule_match(self):
        # 正则层无法解析「换成毒舌吗」（吗 被收进 target）；这类问句由决策模型层处理，
        # 正则层维持不命中。见 Task 4/7 的 ruling。
        store = self.open_store()
        rs = load_ruleset(store)
        self.assertIsNone(rs.match(normalize("毒舌是什么意思")))
        self.assertIsNone(rs.match(normalize("换成毒舌模式好吗？")))
        self.assertIsNone(rs.match(normalize("换成毒舌吗")))

    def test_unknown_name_is_not_command(self):
        store = self.open_store()
        rs = load_ruleset(store)
        self.assertIsNone(rs.match(normalize("换成海盗模式")))
        self.assertIsNone(rs.match(normalize("")))
        self.assertIsNone(rs.match(normalize("今天天气怎么样")))

    def test_persona_wins_over_provider(self):
        store = self.open_store()
        store.upsert_provider({"name": "温柔", "aliases": "", "base_url": "https://x",
                               "api_key": "sk", "model": "m"})
        rs = load_ruleset(store)
        hit = rs.match("换成温柔")
        self.assertEqual(hit.entity["kind"], "persona")


class GoldenCasesTest(unittest.TestCase):
    """黄金语料里 route=rule/none 的句子用真实 Store + 关闭决策模型本地判定。
    model 类句子依赖真实决策接口，不进 unittest（见 scripts/eval_routing.py）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "hub.db"))
        self.store.set_setting("decision_enabled", "0")  # 关闭决策，只测 rule/none

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_golden_cases_route_local(self):
        path = Path(__file__).with_name("fixtures") / "routing_cases.json"
        cases = json.loads(path.read_text("utf-8"))
        service = ChatService(self.store, web_ground=None)
        for case in cases:
            if case["route"] not in ("rule", "none"):
                continue
            with self.subTest(text=case["text"]):
                route = asyncio.run(service._route(case["text"]))
                self.assertEqual((route.route, route.intent_key or "qa"),
                                 (case["route"], case["intent_key"] or "qa"),
                                 case["text"])


if __name__ == "__main__":
    unittest.main()
