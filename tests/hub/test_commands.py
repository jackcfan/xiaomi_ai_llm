import unittest

from hub.commands import Command, parse_command

PERSONAS = [
    {"id": 1, "name": "默认", "aliases": ""},
    {"id": 2, "name": "温柔", "aliases": ""},
    {"id": 3, "name": "毒舌", "aliases": ""},
    {"id": 4, "name": "撒娇", "aliases": ""},
]
PROVIDERS = [
    {"id": 1, "name": "DeepSeek", "aliases": "deepseek"},
    {"id": 2, "name": "Kimi", "aliases": "kimi,月之暗面"},
]


def parse(text):
    return parse_command(text, PERSONAS, PROVIDERS)


class ParseCommandTest(unittest.TestCase):
    def test_switch_persona(self):
        self.assertEqual(parse("换成毒舌模式"), Command("persona", 3, "毒舌"))
        self.assertEqual(parse("小爱同学，切换到温柔"), Command("persona", 2, "温柔"))

    def test_switch_provider_case_insensitive(self):
        self.assertEqual(parse("切换到kimi"), Command("provider", 2, "Kimi"))
        self.assertEqual(parse("换成月之暗面"), Command("provider", 2, "Kimi"))
        self.assertEqual(parse("换成DEEPSEEK模型。"), Command("provider", 1, "DeepSeek"))

    def test_reset_phrases(self):
        for text in ("忘掉刚才的对话", "换个话题", "重新开始", "小爱同学，忘记刚才的对话。"):
            with self.subTest(text=text):
                cmd = parse(text)
                self.assertIsNotNone(cmd)
                self.assertEqual(cmd.kind, "reset")

    def test_question_mentioning_persona_is_not_command(self):
        self.assertIsNone(parse("毒舌是什么意思"))
        self.assertIsNone(parse("换成毒舌模式好吗？"))
        self.assertIsNone(parse("换成毒舌吗"))

    def test_unknown_name_is_not_command(self):
        self.assertIsNone(parse("换成海盗模式"))
        self.assertIsNone(parse(""))
        self.assertIsNone(parse("今天天气怎么样"))

    def test_persona_wins_over_provider(self):
        providers = PROVIDERS + [{"id": 9, "name": "温柔", "aliases": ""}]
        self.assertEqual(
            parse_command("换成温柔", PERSONAS, providers), Command("persona", 2, "温柔")
        )


if __name__ == "__main__":
    unittest.main()
