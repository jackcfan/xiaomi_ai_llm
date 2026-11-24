"""决策正则表：按 priority 加载 active 规则，fullmatch + 长度闸门匹配，实体槽解析。

规则模式只写句式不写实体名；带参数的意图用一个命名分组 ``(?P<target>...)`` 捕获，
运行时再交给 ``resolve_entity``（先角色后模型）解析。解析不到就当未命中、继续下一条。

RuleSet 是纯函数、不碰数据库；命中后的计数回写（``bump_rule_stats``）由调用方负责。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from hub.store import Store

logger = logging.getLogger(__name__)

PUNCT = " \t\r\n，。？！、；：,.?!;:~～…\"'“”‘’"
WAKE_PREFIX = "小爱同学"

# 需要实体参数的意图（与 hub/intents.py 的 param_kind 对应；这里显式列出来，
# 避免 RuleSet 反向依赖 intents 表——匹配器只需要知道「哪些意图要解析实体」。
ENTITY_INTENT_KEYS = ("switch_target",)


def normalize(text: str | None) -> str:
    s = (text or "").strip(PUNCT)
    if s.startswith(WAKE_PREFIX):
        s = s[len(WAKE_PREFIX):].strip(PUNCT)
    return s


@dataclass
class Rule:
    id: int
    intent_key: str
    pattern: str
    max_text_len: int
    source: str
    status: str
    priority: int
    regex: re.Pattern


@dataclass
class RuleHit:
    rule: Rule
    target: str | None
    entity: dict | None


def _names(item: dict) -> list[str]:
    names = [item.get("name") or ""]
    names += (item.get("aliases") or "").replace("，", ",").split(",")
    return [n.strip().casefold() for n in names if n.strip()]


def resolve_entity(store: Store, target: str) -> dict | None:
    """按 name/aliases casefold 精确匹配；先 personas 后 providers。"""
    key = (target or "").strip().casefold()
    if not key:
        return None
    for item in store.list_personas():
        if key in _names(item):
            return {"kind": "persona", "id": item["id"], "name": item["name"]}
    for item in store.list_providers():
        if key in _names(item):
            return {"kind": "provider", "id": item["id"], "name": item["name"]}
    return None


class RuleSet:
    def __init__(
        self,
        rows: list[dict],
        entity_intents: tuple[str, ...] = ENTITY_INTENT_KEYS,
        resolve=None,
    ):
        """rows 已按 priority DESC/id ASC 排好（list_rules 保证）；编译失败的行跳过。"""
        self._entity_intents = set(entity_intents)
        self._resolve = resolve or (lambda t: None)
        self.rules: list[Rule] = []
        for row in rows:
            try:
                regex = re.compile(row["pattern"])
            except re.error as exc:
                logger.warning("[RULES] rule %s has invalid pattern: %s", row.get("id"), exc)
                continue
            self.rules.append(Rule(
                id=row["id"],
                intent_key=row["intent_key"],
                pattern=row["pattern"],
                max_text_len=row.get("max_text_len") or 40,
                source=row.get("source") or "manual",
                status=row.get("status") or "active",
                priority=row.get("priority") or 0,
                regex=regex,
            ))

    def match(self, norm: str | None) -> RuleHit | None:
        if not norm or not norm.strip():
            return None
        for rule in self.rules:
            if len(norm) > rule.max_text_len:
                continue
            m = rule.regex.fullmatch(norm)
            if m is None:
                continue
            target = None
            entity = None
            if rule.intent_key in self._entity_intents:
                target = m.groupdict().get("target")
                if target is None:
                    continue
                entity = self._resolve(target)
                if entity is None:
                    continue
            return RuleHit(rule=rule, target=target, entity=entity)
        return None


def load_ruleset(store: Store) -> RuleSet:
    rows = store.list_rules(status="active")
    return RuleSet(rows, resolve=lambda t: resolve_entity(store, t))
