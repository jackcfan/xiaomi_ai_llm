import unittest

from hub.context import select_context


def turn(ts, kind="qa", interrupted=0, idx=None):
    n = idx if idx is not None else int(ts)
    return {
        "id": n,
        "ts": float(ts),
        "user_text": f"q{n}",
        "answer": f"a{n}",
        "kind": kind,
        "interrupted": interrupted,
        "provider_name": "DeepSeek",
        "persona_name": "默认",
        "web_sources": None,
    }


def newest_first(turns):
    return sorted(turns, key=lambda t: t["ts"], reverse=True)


class SelectContextTest(unittest.TestCase):
    def test_chain_within_gap(self):
        turns = newest_first([turn(0), turn(200), turn(400)])
        msgs = select_context(turns, now=500, gap_seconds=300, max_turns=10)
        self.assertEqual(len(msgs), 6)
        self.assertEqual(msgs[0], {"role": "user", "content": "q0"})
        self.assertEqual(msgs[1], {"role": "assistant", "content": "a0"})
        self.assertEqual(msgs[-2], {"role": "user", "content": "q400"})
        self.assertEqual(msgs[-1], {"role": "assistant", "content": "a400"})

    def test_gap_breaks_chain(self):
        turns = newest_first([turn(0), turn(400)])
        msgs = select_context(turns, now=500, gap_seconds=300, max_turns=10)
        self.assertEqual(msgs, [
            {"role": "user", "content": "q400"},
            {"role": "assistant", "content": "a400"},
        ])

    def test_now_too_far_returns_empty(self):
        turns = [turn(0)]
        self.assertEqual(select_context(turns, now=301, gap_seconds=300, max_turns=10), [])

    def test_now_exactly_at_gap_included(self):
        turns = [turn(0)]
        msgs = select_context(turns, now=300, gap_seconds=300, max_turns=10)
        self.assertEqual(len(msgs), 2)

    def test_reset_stops_chain(self):
        turns = newest_first([turn(100), turn(200, kind="reset"), turn(300)])
        msgs = select_context(turns, now=310, gap_seconds=300, max_turns=10)
        self.assertEqual(msgs, [
            {"role": "user", "content": "q300"},
            {"role": "assistant", "content": "a300"},
        ])

    def test_commands_skipped(self):
        turns = newest_first([turn(100), turn(200, kind="command"), turn(300)])
        msgs = select_context(turns, now=310, gap_seconds=300, max_turns=10)
        self.assertEqual([m["content"] for m in msgs], ["q100", "a100", "q300", "a300"])

    def test_max_turns(self):
        turns = newest_first([turn(i * 10, idx=i) for i in range(12)])
        msgs = select_context(turns, now=120, gap_seconds=300, max_turns=10)
        self.assertEqual(len(msgs), 20)
        self.assertEqual(msgs[0]["content"], "q2")
        self.assertEqual(msgs[-1]["content"], "a11")

    def test_interrupted_included(self):
        turns = [turn(100, interrupted=1)]
        msgs = select_context(turns, now=110, gap_seconds=300, max_turns=10)
        self.assertEqual(msgs[-1], {"role": "assistant", "content": "a100"})

    def test_empty_turns(self):
        self.assertEqual(select_context([], now=0, gap_seconds=300, max_turns=10), [])


if __name__ == "__main__":
    unittest.main()
