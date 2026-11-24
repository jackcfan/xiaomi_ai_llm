"""学习任务：把决策模型判过、正则没接住的样本，蒸馏成新的正则规则。

``validate_candidate`` 是纯函数——模型生成的 pattern 一律不可信，必须通过这里的
全部校验才能入库：语法白名单、含中文、fullmatch 命中率、不命中负例、与已有规则不重复，
并从命中样本算出 max_text_len（模型给的数字只作参考）。

``run_learning`` 是完整的离线学习流程：取未学习样本 → 按意图分组 → 调生成模型
（JSON 模式）产出候选规则 → 逐条校验 → 入库 → 标记样本状态。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from hub.store import Store

logger = logging.getLogger(__name__)

# 学习器可接受的 pattern 长度上限。
MAX_PATTERN_LEN = 200
# 被禁止的语法：零宽断言与反向引用（递归回溯 + 不可控复杂度）。
_FORBIDDEN = ("(?=", "(?!", "(?<=", "(?<!", "\\1", "\\2", "\\3")
# max_text_len 的夹取范围。
MIN_TEXT_LEN = 8
MAX_TEXT_LEN = 40
LEN_SLACK = 6

_CJK = re.compile(r"[一-鿿]")


def validate_candidate(
    pattern: str,
    model_max_len: int | None,
    positives: list[str],
    negatives: list[str],
    existing: list[dict],
) -> tuple[int, str | None]:
    """返回 (max_text_len, None) 表示通过，(0, 原因) 表示拒绝。"""
    if not isinstance(pattern, str):
        return 0, "invalid_regex"
    stripped = pattern.strip()
    if len(stripped) > MAX_PATTERN_LEN:
        return 0, "too_long"
    if any(f in stripped for f in _FORBIDDEN):
        return 0, "forbidden_syntax"
    try:
        regex = re.compile(stripped)
    except re.error:
        return 0, "invalid_regex"
    if not _CJK.search(stripped):
        return 0, "no_cjk"

    # 归一化空白后与已有规则判重。
    normalized = re.sub(r"\s+", "", stripped)
    for rule in existing:
        if re.sub(r"\s+", "", rule.get("pattern") or "") == normalized:
            return 0, "duplicate"

    if not positives:
        return 0, "low_hit_rate"
    hits = [p for p in positives if regex.fullmatch(p)]
    if len(hits) / len(positives) < 0.5:
        return 0, "low_hit_rate"

    for n in negatives:
        if regex.fullmatch(n):
            return 0, "matches_negative"

    observed_max = max(len(h) for h in hits)
    if not isinstance(model_max_len, int) or isinstance(model_max_len, bool):
        model_max_len = None  # 模型输出的长度不可信：字符串/bool/None 一律当没给
    max_len = max(observed_max + LEN_SLACK, model_max_len or 0)
    max_len = max(MIN_TEXT_LEN, min(MAX_TEXT_LEN, max_len))
    return max_len, None


@dataclass
class LearnReport:
    scanned: int = 0
    proposed: int = 0
    accepted: int = 0
    rejected: list[dict] = field(default_factory=list)
    added_rule_ids: list[int] = field(default_factory=list)


_GEN_SYSTEM = (
    "你是正则规则生成器。只输出一个 JSON 对象：{\"rules\":[{\"intent_key\":字符串,"
    "\"pattern\":字符串,\"max_text_len\":整数}]}。约束：pattern 用于 re.fullmatch（隐含全串匹配，"
    "不要再写 ^ 和 $）；最多一个命名分组 (?P<target>...)；禁止零宽断言、反向引用、嵌套量词；"
    "长度不超过 200 字符；必须含中文；只写句式不写死实体名（人名/模型名留给 (?P<target>...)）。"
)


def _entities_catalog(store: Store) -> dict:
    personas = []
    for p in store.list_personas():
        names = [p.get("name") or ""]
        names += (p.get("aliases") or "").replace("，", ",").split(",")
        personas.extend(n.strip() for n in names if n.strip())
    providers = []
    for p in store.list_providers():
        names = [p.get("name") or ""]
        names += (p.get("aliases") or "").replace("，", ",").split(",")
        providers.extend(n.strip() for n in names if n.strip())
    return {"personas": personas, "providers": providers}


def _group_samples(turns: list[dict]) -> dict[str, dict]:
    """把样本按 intent_key 分组为正例/负例。正例 = route in (rule,model) 且非 qa 且未纠正；
    负例 = route=model 且 intent_key=qa。"""
    groups: dict[str, dict] = {}
    for t in turns:
        route = t.get("route")
        intent_key = t.get("intent_key") or ""
        text = t.get("user_text") or ""
        if not text:
            continue
        if route in ("rule", "model") and intent_key not in ("", "qa") and not t.get("corrected"):
            g = groups.setdefault(intent_key, {"positives": [], "negatives": []})
            if text not in g["positives"]:
                g["positives"].append(text)
        elif route == "model" and intent_key == "qa":
            # 负例不按意图分，但生成时按意图组附带一份公共负例。
            pass
    return groups


async def run_learning(
    store: Store,
    generate: Callable[[str, str], Awaitable[str]],
    max_samples: int = 200,
) -> LearnReport | None:
    """取未学习样本，调生成模型产出候选规则，校验后入库。模型输出解析失败返回 None。"""
    turns = store.turns_for_learning(max_samples)
    if not turns:
        return LearnReport()

    groups = _group_samples(turns)
    if not groups:
        # 没有可学习的正例；标记这些样本为 skipped，避免反复取到。
        ids = [t["id"] for t in turns]
        store.set_turn_learn_state(ids, "skipped")
        return LearnReport(scanned=len(turns))

    all_negatives = store.recent_qa_texts(time.time() - 30 * 86400, 500)
    entities = _entities_catalog(store)
    active_rules = [r for r in store.list_rules() if r.get("status") == "active"]

    report = LearnReport(scanned=len(turns))
    used_ids: list[int] = []
    for intent_key, g in groups.items():
        intent = store.get_intent(intent_key)
        instructions = (intent.get("instructions") or "") if intent else ""
        param_kind = (intent.get("param_kind") or "none") if intent else "none"
        positives = g["positives"][:30]
        user_payload = json.dumps({
            "intent_key": intent_key,
            "instructions": instructions,
            "param_kind": param_kind,
            "entities": entities,
            "existing_patterns": [r.get("pattern") for r in active_rules][:20],
            "positives": positives,
            "negatives": all_negatives[:30],
        }, ensure_ascii=False)
        try:
            raw = await generate(_GEN_SYSTEM, user_payload)
            parsed = json.loads(raw)
            candidates = parsed.get("rules") if isinstance(parsed, dict) else None
            if not isinstance(candidates, list):
                raise ValueError("no rules array")
        except Exception as exc:
            logger.warning("[LEARN] generate/parse failed for %s: %s", intent_key, type(exc).__name__)
            return None  # 不改任何样本状态，接口层转 500

        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            pattern = cand.get("pattern")
            model_len = cand.get("max_text_len")
            report.proposed += 1
            max_len, reason = validate_candidate(
                pattern, model_len, positives, all_negatives, active_rules)
            if reason is not None:
                report.rejected.append({"intent_key": intent_key, "pattern": pattern or "",
                                        "reason": reason})
                continue
            rule_id = store.upsert_rule({
                "intent_key": intent_key,
                "pattern": pattern,
                "max_text_len": max_len,
                "source": "learned",
                "status": "active",
                "priority": 50,
                "note": "由学习任务生成",
            })
            report.accepted += 1
            report.added_rule_ids.append(rule_id)
            active_rules.append({"pattern": pattern})

    # 参与过学习的正例标记 used；route='none'（无标签）标 skipped。
    positive_ids = [t["id"] for t in turns
                    if t.get("route") in ("rule", "model")
                    and t.get("intent_key") not in ("", "qa")]
    negative_ids = [t["id"] for t in turns
                    if t.get("route") == "model" and t.get("intent_key") == "qa"]
    store.set_turn_learn_state(positive_ids + negative_ids, "used")
    skipped_ids = [t["id"] for t in turns
                   if t.get("route") == "none" or not t.get("intent_key")]
    store.set_turn_learn_state(skipped_ids, "skipped")
    return report
