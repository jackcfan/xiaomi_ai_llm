import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from hub.store import Store


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


if __name__ == "__main__":
    unittest.main()
