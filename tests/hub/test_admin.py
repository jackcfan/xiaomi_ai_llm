import json
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from hub.app import create_app
from hub.store import Store


class AdminTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "hub.db"))
        self.client = TestClient(create_app(self.store))
        self.provider_id = self.store.list_providers()[0]["id"]

    def tearDown(self):
        self.client.close()
        self.store.close()
        self.tmp.cleanup()

    def post(self, path, data):
        return self.client.post(path, data=data, follow_redirects=False)

    # --- brief tests -----------------------------------------------------

    def test_admin_masks_key(self):
        self.store.upsert_provider({"id": self.provider_id, "api_key": "sk-secret-1234"})
        resp = self.client.get("/admin/providers")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("****1234", resp.text)
        self.assertNotIn("sk-secret", resp.text)

    def test_edit_provider_blank_key_keeps_old(self):
        self.store.upsert_provider({"id": self.provider_id, "api_key": "sk-old-9999"})
        resp = self.post("/admin/providers", {
            "action": "save", "id": str(self.provider_id), "name": "DS2",
            "aliases": "ds", "base_url": "https://x", "api_key": "",
            "model": "m1", "temperature": "", "max_tokens": "",
        })
        self.assertEqual(resp.status_code, 303)
        row = self.store.get_provider(self.provider_id)
        self.assertEqual(row["api_key"], "sk-old-9999")
        self.assertEqual(row["name"], "DS2")
        self.assertEqual(row["model"], "m1")

    def test_switch_current_persona_via_form(self):
        target = self.store.list_personas()[2]
        resp = self.post("/admin", {"current_persona_id": str(target["id"])})
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.store.current_persona()["id"], target["id"])

    def test_history_page_escapes_html(self):
        self.store.add_turn("<script>", "答", "DeepSeek", "默认", "qa")
        resp = self.client.get("/admin/history")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("&lt;script&gt;", resp.text)
        self.assertNotIn("<script>", resp.text)

    def test_context_settings_validated(self):
        resp = self.post("/admin", {"context_gap_seconds": "abc"})
        self.assertEqual(self.store.get_setting("context_gap_seconds"), "300")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("context_gap_seconds", resp.text)

    # --- extra coverage --------------------------------------------------

    def test_empty_key_shows_unset(self):
        self.store.upsert_provider({"id": self.provider_id, "api_key": ""})
        self.assertIn("未设置", self.client.get("/admin/providers").text)

    def test_overview_shows_settings_and_token(self):
        token = self.store.get_setting("device_token")
        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(token, resp.text)
        self.assertIn("DeepSeek", resp.text)

    def test_regenerate_device_token(self):
        old = self.store.get_setting("device_token")
        resp = self.post("/admin", {"action": "regen_token"})
        self.assertEqual(resp.status_code, 303)
        new = self.store.get_setting("device_token")
        self.assertTrue(new)
        self.assertNotEqual(old, new)

    def test_update_context_and_base_prompt(self):
        resp = self.post("/admin", {
            "context_gap_seconds": "120", "context_max_turns": "6",
            "base_prompt": "新的基础提示",
        })
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.store.get_setting("context_gap_seconds"), "120")
        self.assertEqual(self.store.get_setting("context_max_turns"), "6")
        self.assertEqual(self.store.get_setting("base_prompt"), "新的基础提示")

    def test_add_and_delete_provider(self):
        resp = self.post("/admin/providers", {
            "action": "save", "id": "", "name": "Kimi", "aliases": "",
            "base_url": "https://k", "api_key": "sk-kimi-5678", "model": "k1",
            "temperature": "0.5", "max_tokens": "512",
        })
        self.assertEqual(resp.status_code, 303)
        new = [p for p in self.store.list_providers() if p["name"] == "Kimi"][0]
        self.assertEqual(new["api_key"], "sk-kimi-5678")
        self.assertEqual(new["temperature"], 0.5)
        self.assertEqual(new["max_tokens"], 512)
        self.post("/admin/providers", {"action": "delete", "id": str(new["id"])})
        self.assertIsNone(self.store.get_provider(new["id"]))

    def test_invalid_provider_number_rejected(self):
        resp = self.post("/admin/providers", {
            "action": "save", "id": "", "name": "Bad", "temperature": "hot",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(any(p["name"] == "Bad" for p in self.store.list_providers()))

    def provider_form(self, **overrides):
        data = {
            "action": "save", "id": "", "name": "New", "aliases": "",
            "base_url": "https://n", "api_key": "sk-new-0000", "model": "n1",
            "temperature": "", "max_tokens": "",
        }
        data.update(overrides)
        return data

    def assert_rejected(self, resp):
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(any(p["name"] == "New" for p in self.store.list_providers()))

    def test_provider_temperature_range_validated(self):
        for bad in ("-0.1", "2.5", "nan", "inf", "hot"):
            with self.subTest(temperature=bad):
                self.assert_rejected(self.post("/admin/providers", self.provider_form(temperature=bad)))
        resp = self.post("/admin/providers", self.provider_form(temperature="2"))
        self.assertEqual(resp.status_code, 303)

    def test_provider_max_tokens_must_be_positive_int(self):
        for bad in ("0", "-5", "1.5", "many"):
            with self.subTest(max_tokens=bad):
                self.assert_rejected(self.post("/admin/providers", self.provider_form(max_tokens=bad)))
        resp = self.post("/admin/providers", self.provider_form(max_tokens="1"))
        self.assertEqual(resp.status_code, 303)

    def test_provider_extra_body_saved_and_rendered(self):
        resp = self.post("/admin/providers", self.provider_form(
            extra_body='{"thinking": {"type": "disabled"}}'))
        self.assertEqual(resp.status_code, 303)
        new = [p for p in self.store.list_providers() if p["name"] == "New"][0]
        self.assertEqual(json.loads(new["extra_body"]), {"thinking": {"type": "disabled"}})
        page = self.client.get("/admin/providers").text
        self.assertIn("name='extra_body'", page)
        self.assertIn("&quot;thinking&quot;", page)

    def test_provider_extra_body_invalid_json_rejected(self):
        for bad in ("{thinking:", "[1, 2]", '"x"'):
            with self.subTest(extra_body=bad):
                resp = self.post("/admin/providers", self.provider_form(extra_body=bad))
                self.assert_rejected(resp)
                self.assertIn("附加请求体", resp.text)
        # 编辑已有供应商时非法 JSON 也不写入任何字段
        before = self.store.get_provider(self.provider_id)
        resp = self.post("/admin/providers", self.provider_form(
            id=str(self.provider_id), name="Changed", extra_body="{bad"))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.store.get_provider(self.provider_id), before)

    def test_provider_extra_body_kept_when_absent_cleared_when_blank(self):
        self.post("/admin/providers", self.provider_form(
            id=str(self.provider_id), name="DeepSeek", api_key=""))  # 表单不带 extra_body
        self.assertIsNotNone(self.store.get_provider(self.provider_id)["extra_body"])
        self.post("/admin/providers", self.provider_form(
            id=str(self.provider_id), name="DeepSeek", api_key="", extra_body="  "))
        self.assertIsNone(self.store.get_provider(self.provider_id)["extra_body"])

    def test_cross_origin_post_rejected(self):
        before = self.store.get_provider(self.provider_id)
        token = self.store.get_setting("device_token")
        cases = [
            {"Origin": "http://evil.example"},
            {"Origin": "http://testserver:8080"},
            {"Origin": "null"},
            {"Referer": "http://evil.example/page"},
        ]
        for headers in cases:
            with self.subTest(headers=headers):
                resp = self.client.post("/admin/providers", data=self.provider_form(
                    id=str(self.provider_id), base_url="https://evil.example/v1",
                ), headers=headers, follow_redirects=False)
                self.assertEqual(resp.status_code, 403)
                resp = self.client.post("/admin", data={"action": "regen_token"},
                                        headers=headers, follow_redirects=False)
                self.assertEqual(resp.status_code, 403)
                resp = self.client.post("/admin/personas", data={"action": "save", "name": "Evil"},
                                        headers=headers, follow_redirects=False)
                self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.store.get_provider(self.provider_id), before)
        self.assertEqual(self.store.get_setting("device_token"), token)
        self.assertFalse(any(p["name"] == "Evil" for p in self.store.list_personas()))

    def test_same_origin_and_headerless_post_accepted(self):
        cases = [
            {"Origin": "http://testserver"},
            {"Referer": "http://testserver/admin/providers"},
            {},
        ]
        for i, headers in enumerate(cases):
            with self.subTest(headers=headers):
                resp = self.client.post("/admin/personas", data={
                    "action": "save", "name": f"P{i}", "voice": "zh-CN-YunxiNeural",
                }, headers=headers, follow_redirects=False)
                self.assertEqual(resp.status_code, 303)
                self.assertTrue(any(p["name"] == f"P{i}" for p in self.store.list_personas()))

    def test_delete_current_provider_and_persona_pages_still_render(self):
        persona_id = self.store.current_persona()["id"]
        self.post("/admin/providers", {"action": "delete", "id": str(self.provider_id)})
        self.post("/admin/personas", {"action": "delete", "id": str(persona_id)})
        for path in ("/admin", "/admin/providers", "/admin/personas", "/admin/history"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_persona_voice_options_and_custom_voice(self):
        page = self.client.get("/admin/personas").text
        self.assertIn("zh-CN-YunyangNeural", page)
        resp = self.post("/admin/personas", {
            "action": "save", "id": "", "name": "海盗", "aliases": "",
            "prompt": "啊哈", "voice": "zh-CN-XiaoxiaoNeural",
            "voice_custom": "zh-TW-HsiaoChenNeural",
        })
        self.assertEqual(resp.status_code, 303)
        new = [p for p in self.store.list_personas() if p["name"] == "海盗"][0]
        self.assertEqual(new["voice"], "zh-TW-HsiaoChenNeural")

    def test_history_pagination(self):
        for i in range(55):
            self.store.add_turn(f"问题{i:02d}", "答", "P", "默认", "qa", ts=1000 + i)
        first = self.client.get("/admin/history").text
        self.assertIn("问题54", first)
        self.assertNotIn("问题04", first)
        second = self.client.get("/admin/history?page=2").text
        self.assertIn("问题04", second)
        self.assertNotIn("问题54", second)


if __name__ == "__main__":
    unittest.main()
