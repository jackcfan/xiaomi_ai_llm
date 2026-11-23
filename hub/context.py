"""Pick recent conversation turns to send to the LLM as context."""


def select_context(turns: list[dict], now: float, gap_seconds: int, max_turns: int) -> list[dict]:
    """Build OpenAI-style messages from turns (newest first, as Store.recent_turns returns).

    Walk from newest to oldest. A turn belongs to the current session only if the gap
    to the previous point (now for the newest turn, else the newer neighbor) is
    <= gap_seconds. A "reset" turn ends the walk and is excluded. "command" turns
    keep the chain alive but are not included. At most max_turns turns are taken.
    Interrupted answers are included as-is. Result is oldest first.
    """
    picked: list[dict] = []
    prev_ts = float(now)
    for t in turns:
        if len(picked) >= max_turns:
            break
        ts = float(t["ts"])
        if prev_ts - ts > gap_seconds:
            break
        if t.get("kind") == "reset":
            break
        prev_ts = ts
        if t.get("kind") == "command":
            continue
        picked.append(t)

    messages: list[dict] = []
    for t in reversed(picked):
        messages.append({"role": "user", "content": t["user_text"]})
        messages.append({"role": "assistant", "content": t["answer"]})
    return messages
