import asyncio
import os
import tempfile
import unittest

from hub.learn import validate_candidate, run_learning
from hub.store import Store


def fake_gen(pattern):
    async def gen(system, user):
        return '{"rules": [{"intent_key": "switch_target", "pattern": "%s", "max_text_len": 20}]}' % pattern

    return gen


def raising_gen():
    async def gen(system, user):
        return "not json"

    return gen


class ValidateCandidateTest(unittest.TestCase):
    def test_accepts_generalized_pattern(self):
        maxlen, reason = validate_candidate(
            r"^(?:把)?角色(?:切换|换)(?:成|到|为)(?P<target>.+)$",
            None, ["角色切换到银月", "角色换成毒舌"], ["今天天气怎么样"], [])
        self.assertIsNone(reason)
        self.assertEqual(maxlen, 13)  # 最长命中样本 7 字 + 6，夹在 [8, 40]

    def test_rejects_invalid_regex(self):
        self.assertEqual(validate_candidate("([", None, ["a"], [], [])[1], "invalid_regex")

    def test_rejects_lookaround(self):
        self.assertEqual(validate_candidate(r"^(?=切换).+$", None, ["切换"], [], [])[1],
                         "forbidden_syntax")

    def test_rejects_backreference(self):
        self.assertEqual(validate_candidate(r"^(\w+)\1$", None, ["abab"], [], [])[1],
                         "forbidden_syntax")

    def test_rejects_pattern_without_cjk(self):
        self.assertEqual(validate_candidate(r"^switch$", None, ["switch"], [], [])[1], "no_cjk")

    def test_rejects_too_long_pattern(self):
        self.assertEqual(validate_candidate("^" + "x" * 201 + "$", None, ["x" * 201], [], [])[1],
                         "too_long")

    def test_rejects_pattern_matching_negative(self):
        self.assertEqual(validate_candidate(r"^换(?:成|到)(?P<target>.+)$", None,
                                            ["换成毒舌"], ["换成什么好"], [])[1], "matches_negative")

    def test_rejects_duplicate_of_existing(self):
        existing = [{"pattern": r"^换成(?P<target>.+)$"}]
        self.assertEqual(validate_candidate(r"^换成(?P<target>.+)$", None,
                                            ["换成毒舌"], [], existing)[1], "duplicate")

    def test_rejects_low_hit_rate(self):
        self.assertEqual(validate_candidate(r"^换成(?P<target>毒舌)$", None,
                                            ["换成毒舌", "换成银月", "换个温柔"], [], [])[1],
                         "low_hit_rate")

    def test_max_len_clamped_high(self):
        self.assertEqual(validate_candidate(r"^换(?P<target>.+)$", 99, ["换毒"], [], [])[0], 40)

    def test_max_len_clamped_low(self):
        # 命中样本极短 + model_max_len 也小 → 夹到下限 8
        self.assertEqual(validate_candidate(r"^换(?P<target>.+)$", 1, ["换毒"], [], [])[0], 8)

    def test_model_max_len_respected_when_larger(self):
        # 命中样本 2 字，model_max_len=20 → 取 max(2+6, 20)=20
        self.assertEqual(validate_candidate(r"^换(?P<target>.+)$", 20, ["换毒"], [], [])[0], 20)

    def test_model_max_len_string_is_ignored(self):
        # 审查发现：模型可能输出 "max_text_len": "18"（字符串）→ 原 max() 会 TypeError。
        # 现在非 int 的 model_max_len 一律当没给。
        self.assertEqual(validate_candidate(r"^换(?P<target>.+)$", "18", ["换毒"], [], [])[0], 8)


class RunLearningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "hub.db")
        self.stores = []

    def tearDown(self):
        for s in self.stores:
            s.close()
        self.tmp.cleanup()

    def open_store(self):
        store = Store(self.path)
        self.stores.append(store)
        return store

    def seed_turn(self, store, text, route, intent_key):
        store.add_turn(text, "答", "DeepSeek", "默认", "qa" if route == "model" else "command",
                       route=route, intent_key=intent_key, learn_state="new", ts=1000.0)

    def test_inserts_active_learned_rule(self):
        store = self.open_store()
        self.seed_turn(store, "角色切换到银月", "model", "switch_target")
        report = asyncio.run(run_learning(store, fake_gen(r"^(?:把)?角色切换到(?P<target>.+)$")))
        self.assertEqual((report.scanned, report.proposed, report.accepted), (1, 1, 1))
        rule = store.get_rule(report.added_rule_ids[0])
        self.assertEqual((rule["source"], rule["status"], rule["priority"]),
                         ("learned", "active", 50))

    def test_second_run_is_idempotent(self):
        store = self.open_store()
        self.seed_turn(store, "角色切换到银月", "model", "switch_target")
        pattern = r"^(?:把)?角色切换到(?P<target>.+)$"
        asyncio.run(run_learning(store, fake_gen(pattern)))
        second = asyncio.run(run_learning(store, fake_gen(pattern)))
        self.assertEqual(second.scanned, 0)  # 样本已标记 used
        self.assertEqual(len(store.list_rules()), 3)

    def test_empty_corpus_returns_zero_report(self):
        store = self.open_store()
        report = asyncio.run(run_learning(store, fake_gen("x")))
        self.assertEqual((report.scanned, report.accepted, report.added_rule_ids), (0, 0, []))

    def test_provider_failure_returns_none(self):
        store = self.open_store()
        self.seed_turn(store, "角色切换到银月", "model", "switch_target")
        self.assertIsNone(asyncio.run(run_learning(store, raising_gen())))
        # 失败不改样本状态
        self.assertEqual(store.turns_for_learning(10)[0]["learn_state"], "new")

    def test_marks_skipped_vs_used(self):
        store = self.open_store()
        self.seed_turn(store, "角色切换到银月", "model", "switch_target")
        # 一条 route='none' 的无标签样本
        store.add_turn("今天天气怎么样", "答", "DeepSeek", "默认", "qa",
                       route="none", intent_key="", learn_state="new", ts=2000.0)
        asyncio.run(run_learning(store, fake_gen(r"^(?:把)?角色切换到(?P<target>.+)$")))
        states = {t["user_text"]: t["learn_state"] for t in store.list_turns(0, 10)}
        self.assertEqual(states["角色切换到银月"], "used")
        self.assertEqual(states["今天天气怎么样"], "skipped")

    def test_negative_blocks_rule(self):
        store = self.open_store()
        self.seed_turn(store, "角色切换到银月", "model", "switch_target")
        # 负例：一条被判成 qa 的文本，恰好会被候选规则命中 → 拒绝
        store.add_turn("角色切换到什么好", "答", "DeepSeek", "默认", "qa",
                       route="model", intent_key="qa", learn_state="new")  # ts 用真实时间
        report = asyncio.run(run_learning(store, fake_gen(r"^角色切换到(?P<target>.+)$")))
        self.assertEqual((report.proposed, report.accepted), (1, 0))
        self.assertEqual(report.rejected[0]["reason"], "matches_negative")


if __name__ == "__main__":
    unittest.main()
