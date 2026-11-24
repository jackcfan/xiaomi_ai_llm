import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from hub.store import SETTING_DESCRIPTIONS, Store

# 改造前的完整旧 schema（turns 无 route/intent_key/rule_id/decision/learn_state/corrected），
# 用于验证迁移：旧库补齐新列、补上新默认设置而不覆盖已有值。
OLD_SCHEMA = """
CREATE TABLE providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '', base_url TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
    temperature REAL, max_tokens INTEGER, extra_body TEXT
);
CREATE TABLE personas (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '', prompt TEXT NOT NULL DEFAULT '',
    voice TEXT NOT NULL DEFAULT '', tts_provider TEXT NOT NULL DEFAULT ''
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    user_text TEXT NOT NULL DEFAULT '',
    answer TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    interrupted INTEGER NOT NULL DEFAULT 0,
    provider_name TEXT NOT NULL DEFAULT '',
    persona_name TEXT NOT NULL DEFAULT '',
    web_sources TEXT NOT NULL DEFAULT ''
);
CREATE INDEX turns_ts ON turns (ts);
"""


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "hub.db")
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def open_store(self):
        store = Store(self.path)
        self.stores.append(store)
        return store

    def test_seed_defaults(self):
        store = self.open_store()
        names = {p["name"] for p in store.list_personas()}
        self.assertEqual(names, {"默认", "温柔", "毒舌", "撒娇"})
        self.assertEqual(store.get_setting("context_gap_seconds"), "300")
        self.assertEqual(store.current_provider()["name"], "DeepSeek")
        self.assertGreaterEqual(len(store.get_setting("device_token")), 16)

    def test_seed_deepseek_extra_body_disables_thinking(self):
        store = self.open_store()
        provider = store.current_provider()
        self.assertEqual(json.loads(provider["extra_body"]), {"thinking": {"type": "disabled"}})

    def test_migrates_db_without_extra_body_column(self):
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                aliases TEXT NOT NULL DEFAULT '', base_url TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
                temperature REAL, max_tokens INTEGER
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO providers (name, base_url, api_key, model)
                VALUES ('DeepSeek', 'https://api.deepseek.com', 'sk-old', 'deepseek-flash');
            INSERT INTO settings (key, value) VALUES ('current_provider_id', '1');
        """)
        conn.commit()
        conn.close()
        with self.assertLogs("hub.store", "INFO"):
            store = self.open_store()
        provider = store.get_provider(1)
        self.assertIsNone(provider["extra_body"])  # 旧行为 NULL，需在后台手动填写
        self.assertEqual(provider["api_key"], "sk-old")
        self.assertEqual(len(store.list_providers()), 1)  # 已有 settings，不重新播种
        store.upsert_provider({"id": 1, "extra_body": '{"thinking":{"type":"disabled"}}'})
        self.assertEqual(store.get_provider(1)["extra_body"], '{"thinking":{"type":"disabled"}}')
        # 再次打开：列已存在，不再迁移，数据保留
        again = self.open_store()
        self.assertEqual(again.get_provider(1)["extra_body"], '{"thinking":{"type":"disabled"}}')

    def test_seed_only_once(self):
        self.open_store()
        store = self.open_store()
        self.assertEqual(len(store.list_personas()), 4)

    def test_persona_roundtrip_and_current(self):
        store = self.open_store()
        pid = store.upsert_persona(
            {"name": "东北", "aliases": "", "prompt": "p", "voice": "zh-CN-YunxiNeural"}
        )
        store.set_setting("current_persona_id", str(pid))
        self.assertEqual(store.current_persona()["name"], "东北")

    def test_persona_tts_provider_roundtrip_and_engine(self):
        store = self.open_store()
        # 默认人设引擎是 edgetts（空串 → edgetts）
        default = store.list_personas()[0]
        self.assertEqual(store.persona_tts_engine(default), "edgetts")
        # 写入 siliconflow 并回读
        pid = store.upsert_persona({
            "name": "cosy", "aliases": "", "prompt": "p",
            "voice": "speech:sqz:test", "tts_provider": "siliconflow",
        })
        row = store.get_persona(pid)
        self.assertEqual(row["tts_provider"], "siliconflow")
        self.assertEqual(store.persona_tts_engine(row), "siliconflow")
        # 未知值回退 edgetts
        row["tts_provider"] = "bogus"
        self.assertEqual(store.persona_tts_engine(row), "edgetts")

    def test_find_tts_provider_by_name_and_alias(self):
        store = self.open_store()
        self.assertIsNone(store.find_tts_provider("siliconflow"))
        pid = store.upsert_provider({
            "name": "硅基流动", "aliases": "siliconflow,硅基", "base_url": "https://api.siliconflow.cn/v1",
            "api_key": "sk-sf", "model": "deepseek-flash",
        })
        found = store.find_tts_provider("siliconflow")
        self.assertIsNotNone(found)
        self.assertEqual(found["api_key"], "sk-sf")
        # 别名也能命中
        self.assertEqual(store.find_tts_provider("硅基流动")["id"], pid)
        # 大小写不敏感
        self.assertIsNotNone(store.find_tts_provider("SiliconFlow"))

    def test_migrates_personas_without_tts_provider_column(self):
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                aliases TEXT NOT NULL DEFAULT '', base_url TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
                temperature REAL, max_tokens INTEGER, extra_body TEXT
            );
            CREATE TABLE personas (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                aliases TEXT NOT NULL DEFAULT '', prompt TEXT NOT NULL DEFAULT '',
                voice TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO providers (name, base_url, api_key, model)
                VALUES ('DeepSeek', 'https://api.deepseek.com', 'sk-old', 'deepseek-flash');
            INSERT INTO settings (key, value) VALUES ('current_provider_id', '1');
        """)
        conn.commit()
        conn.close()
        store = self.open_store()
        self.assertEqual(store.list_personas(), [])  # 没有 personas 行，只有迁移补列
        # 旧库已有数据时补列不丢数据、默认 edgetts
        cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(personas)")}
        self.assertIn("tts_provider", cols)

    def test_current_falls_back_when_deleted(self):
        store = self.open_store()
        current = store.current_persona()
        store.delete_persona(current["id"])
        with self.assertLogs("hub.store", level="WARNING") as logs:
            fallback = store.current_persona()
        min_id = min(p["id"] for p in store.list_personas())
        self.assertEqual(fallback["id"], min_id)
        message = "\n".join(logs.output)
        self.assertIn("personas", message)
        self.assertIn(str(current["id"]), message)
        self.assertIn(str(min_id), message)

    def test_current_falls_back_on_non_decimal_setting(self):
        store = self.open_store()
        store.set_setting("current_persona_id", "²")
        with self.assertLogs("hub.store", level="WARNING"):
            fallback = store.current_persona()
        self.assertEqual(fallback["id"], min(p["id"] for p in store.list_personas()))

    def test_turns_order_and_flags(self):
        store = self.open_store()
        store.add_turn("q1", "a1", "DeepSeek", "默认", "qa", ts=100)
        store.add_turn("q2", "a2", "DeepSeek", "默认", "qa", ts=200, interrupted=True)
        turns = store.recent_turns(10)
        self.assertEqual(turns[0]["ts"], 200)
        self.assertEqual(turns[0]["interrupted"], 1)

    def test_new_turn_fields_roundtrip(self):
        store = self.open_store()
        store.add_turn("切换成毒舌", "好的，已切换为毒舌", "DeepSeek", "毒舌", "command",
                       route="rule", intent_key="switch_target", rule_id=7,
                       decision='{"confidence": 1.0}', learn_state="new")
        turn = store.list_turns(0, 1)[0]
        self.assertEqual((turn["route"], turn["intent_key"], turn["rule_id"], turn["learn_state"]),
                         ("rule", "switch_target", 7, "new"))
        self.assertEqual(turn["decision"], '{"confidence": 1.0}')
        self.assertEqual(turn["corrected"], 0)

    def test_migrates_old_turns_table(self):
        conn = sqlite3.connect(self.path)
        conn.executescript(OLD_SCHEMA)
        conn.commit()
        conn.close()
        store = self.open_store()
        cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(turns)")}
        self.assertTrue({"route", "intent_key", "rule_id", "decision", "learn_state",
                         "corrected"} <= cols)

    def test_setting_descriptions_seeded_and_rendered(self):
        # 新库与迁移后的旧库都要回填 description；已有非空 description 不被覆盖。
        store = self.open_store()
        self.assertEqual(store.get_setting_description("base_prompt"), SETTING_DESCRIPTIONS["base_prompt"])
        self.assertEqual(store.get_setting_description("current_provider_id"), SETTING_DESCRIPTIONS["current_provider_id"])
        self.assertIn("正整数", store.get_setting_description("context_gap_seconds"))
        self.assertIn("百炼", store.get_setting_description("decision_provider_id"))
        # 描述列落库（settings 表带 description 列）
        cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(settings)")}
        self.assertIn("description", cols)

    def test_setting_description_migrates_old_db(self):
        # 旧 schema 的 settings 表没有 description 列：迁移补列并回填，已有值不覆盖。
        old_path = os.path.join(self.tmpdir, "old.db")
        conn = sqlite3.connect(old_path)
        conn.executescript(OLD_SCHEMA)
        conn.commit()
        conn.close()
        store = Store(old_path)
        self.stores.append(store)
        self.assertEqual(store.get_setting_description("context_gap_seconds"),
                         SETTING_DESCRIPTIONS["context_gap_seconds"])
        self.assertEqual(store.get_setting("context_gap_seconds"), "300")

    def test_setting_description_not_overwritten_when_present(self):
        store = self.open_store()
        store._execute(
            "UPDATE settings SET description = ? WHERE `key` = ?", ("自定义说明", "base_prompt")
        )
        again = Store(self.path)
        self.stores.append(again)
        self.assertEqual(again.get_setting_description("base_prompt"), "自定义说明")

    def test_seed_creates_intents_rules_capabilities(self):
        store = self.open_store()
        self.assertEqual([i["key"] for i in store.list_intents()],
                         ["switch_target", "reset_context", "list_capabilities"])
        self.assertEqual([(r["intent_key"], r["source"]) for r in store.list_rules()],
                         [("reset_context", "manual"), ("switch_target", "manual")])
        self.assertEqual(len(store.list_capabilities()), 3)

    def test_defaults_added_to_existing_db(self):
        old_path = os.path.join(self.tmpdir, "old.db")
        conn = sqlite3.connect(old_path)
        conn.executescript(OLD_SCHEMA)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)", ("current_persona_id", "3")
        )
        conn.commit()
        conn.close()
        store = Store(old_path)
        self.stores.append(store)
        self.assertEqual(store.get_setting("decision_timeout_ms"), "3000")
        self.assertEqual(store.get_setting("current_persona_id"), "3")

    def test_bump_rule_stats(self):
        store = self.open_store()
        rid = store.list_rules()[0]["id"]
        store.bump_rule_stats(rid, hit=True, recheck=True, mismatch=True)
        row = store.get_rule(rid)
        self.assertEqual((row["hit_count"], row["recheck_count"], row["mismatch_count"]),
                         (1, 1, 1))
        self.assertIsNotNone(row["last_hit_ts"])

    def test_deleted_manual_rule_not_reseeded(self):
        # 审查发现：原 _seed_rules 按 pattern 判重、每次启动都播种 → 后台删掉的规则重启复活。
        # 现在只在规则表为空时播种。
        store = self.open_store()
        rid = store.list_rules()[0]["id"]
        store.delete_rule(rid)
        again = Store(self.path)
        self.stores.append(again)
        self.assertEqual(len(again.list_rules()), 1)  # 只剩另一条，被删的不回来


if __name__ == "__main__":
    unittest.main()
