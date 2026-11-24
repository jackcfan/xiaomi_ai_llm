import os
import tempfile
import unittest

from hub.intents import INTENTS, INTENT_KEYS, QA_LABEL, HANDLERS, sync_intents, execute
from hub.store import Store


class IntentsTestCase(unittest.TestCase):
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

    def test_every_intent_has_handler(self):
        self.assertEqual(set(INTENT_KEYS), set(HANDLERS))
        self.assertEqual(QA_LABEL, "qa")

    def test_sync_intents_does_not_overwrite_edited_text(self):
        store = self.open_store()
        store.upsert_intent({"key": "reset_context", "name": "重置", "instructions": "后台改过",
                             "param_kind": "none", "enabled": 1, "priority": 2})
        sync_intents(store)
        self.assertEqual(store.get_intent("reset_context")["instructions"], "后台改过")

    def test_sync_intents_fills_empty_instructions(self):
        store = self.open_store()
        store.upsert_intent({"key": "reset_context", "name": "重置", "instructions": "",
                             "param_kind": "none", "enabled": 1, "priority": 2})
        sync_intents(store)
        self.assertNotEqual(store.get_intent("reset_context")["instructions"], "")

    def test_execute_switch_target(self):
        store = self.open_store()
        pid = store.list_personas()[2]["id"]
        reply, kind = execute(store, "switch_target", {"kind": "persona", "id": pid, "name": "毒舌"})
        self.assertEqual((reply, kind), ("好的，已切换为毒舌", "command"))
        self.assertEqual(store.current_persona()["name"], "毒舌")

    def test_execute_switch_provider(self):
        store = self.open_store()
        pid = store.list_providers()[0]["id"]
        reply, kind = execute(store, "switch_target", {"kind": "provider", "id": pid, "name": "DeepSeek"})
        self.assertEqual((reply, kind), ("好的，已切换到DeepSeek", "command"))
        self.assertEqual(store.current_provider()["name"], "DeepSeek")

    def test_execute_reset_context(self):
        self.assertEqual(execute(self.open_store(), "reset_context", {}),
                         ("好的，我们重新开始", "reset"))

    def test_execute_list_capabilities_skips_disabled(self):
        store = self.open_store()
        store._execute("UPDATE capabilities SET enabled = 0 WHERE name = '切换回答模型'")
        reply, kind = execute(store, "list_capabilities", {})
        self.assertIn("切换角色或音色", reply)
        self.assertNotIn("切换回答模型", reply)
        self.assertEqual(kind, "command")


if __name__ == "__main__":
    unittest.main()
