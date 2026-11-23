import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from hub.app import create_app
from hub.store import Store

URL = "/api/v1/device/fallback"
SECRET = "sk-secret-1234"


class FallbackConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "hub.db"))
        self.deepseek = next(p for p in self.store.list_providers() if p["name"] == "DeepSeek")
        self.store.upsert_provider({"id": self.deepseek["id"], "api_key": SECRET})
        self.token = self.store.get_setting("device_token")
        self.client = TestClient(create_app(self.store))

    def tearDown(self):
        self.client.close()
        self.store.close()
        self.tmp.cleanup()

    def get(self, token=None):
        headers = {} if token is None else {"X-Device-Token": token}
        return self.client.get(URL, headers=headers)

    def test_requires_token(self):
        for token in (None, "", "wrong-token", self.token + "x"):
            with self.subTest(token=token):
                response = self.get(token)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn(SECRET, response.text)

    def test_returns_fallback_provider(self):
        response = self.get(self.token)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"base_url", "api_key", "model", "system_prompt"})
        self.assertTrue(all(isinstance(v, str) for v in body.values()))
        self.assertEqual(body["base_url"], "https://api.deepseek.com")
        self.assertEqual(body["api_key"], SECRET)
        self.assertEqual(body["model"], "deepseek-flash")
        persona = self.store.current_persona()
        self.assertEqual(
            body["system_prompt"],
            self.store.get_setting("base_prompt") + "\n" + persona["prompt"],
        )

    def test_system_prompt_follows_current_persona(self):
        gentle = next(p for p in self.store.list_personas() if p["name"] == "温柔")
        self.store.set_setting("current_persona_id", str(gentle["id"]))
        body = self.get(self.token).json()
        self.assertIn(gentle["prompt"], body["system_prompt"])

    def test_fallback_independent_of_current(self):
        kimi = self.store.upsert_provider({
            "name": "Kimi",
            "base_url": "https://api.moonshot.cn/v1",
            "api_key": "sk-kimi",
            "model": "kimi-k2",
        })
        self.store.set_setting("current_provider_id", str(kimi))
        self.assertEqual(
            self.store.get_setting("fallback_provider_id"), str(self.deepseek["id"])
        )
        body = self.get(self.token).json()
        self.assertEqual(body["api_key"], SECRET)
        self.assertEqual(body["model"], "deepseek-flash")

    def test_missing_fallback_provider_uses_lowest_id(self):
        self.store.set_setting("fallback_provider_id", "999")
        with self.assertLogs("hub.store", level="WARNING"):
            response = self.get(self.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["api_key"], SECRET)

    def test_no_provider_returns_503(self):
        self.store.delete_provider(self.deepseek["id"])
        with self.assertLogs("hub.store", level="WARNING"):
            response = self.get(self.token)
        self.assertEqual(response.status_code, 503)

    def test_empty_device_token_setting_rejects_all(self):
        self.store.set_setting("device_token", "")
        self.assertEqual(self.get("").status_code, 403)


if __name__ == "__main__":
    unittest.main()
