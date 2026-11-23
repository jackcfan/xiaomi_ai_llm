"""Rule-based voice commands: switch persona/provider, reset conversation."""

import re
from dataclasses import dataclass

_PUNCT = " \t\r\n，。？！、；：,.?!;:~～…\"'“”‘’"
_WAKE_PREFIX = "小爱同学"
_RESET_RE = re.compile(r"(?:忘掉刚才的对话|忘记刚才的对话|换个话题|重新开始)")
_SWITCH_RE = re.compile(r"(?:切换到|切换成|换成|改成|换到|用)(.+?)(?:模式|性格|风格|模型|的)?")
_QUESTION_RE = re.compile(r"吗|什么|怎么|为什么|是谁|？|\?")


@dataclass
class Command:
    kind: str  # "persona" | "provider" | "reset"
    target_id: int | None
    target_name: str | None


def _normalize(text: str) -> str:
    s = (text or "").strip(_PUNCT)
    if s.startswith(_WAKE_PREFIX):
        s = s[len(_WAKE_PREFIX):].strip(_PUNCT)
    return s


def _names(item: dict) -> list[str]:
    names = [item.get("name") or ""]
    names += (item.get("aliases") or "").replace("，", ",").split(",")
    return [n.strip().casefold() for n in names if n.strip()]


def _find(target: str, items: list[dict]) -> dict | None:
    key = target.strip().casefold()
    for item in items:
        if key in _names(item):
            return item
    return None


def parse_command(text: str, personas: list[dict], providers: list[dict]) -> Command | None:
    if not text or _QUESTION_RE.search(text):
        return None
    s = _normalize(text)
    if not s:
        return None
    if _RESET_RE.fullmatch(s):
        return Command("reset", None, None)
    m = _SWITCH_RE.fullmatch(s)
    if not m:
        return None
    # Try the full captured name first (handles names ending in a suffix word),
    # then fall back to the lazy capture with the optional suffix removed.
    candidates = [s[m.start(1):], m.group(1)]
    for kind, items in (("persona", personas), ("provider", providers)):
        for cand in candidates:
            item = _find(cand, items)
            if item is not None:
                return Command(kind, item["id"], item["name"])
    return None
