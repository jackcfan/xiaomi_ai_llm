"""百炼 decision-model-preview 判定客户端。

该 endpoint 不是 OpenAI 兼容格式：POST {base}/compatible-mode/v1/systemone，头 ``authorization``
不带 Bearer 前缀，体为声明式 ``{model, state:{content}, questions:{...}}``，返回
``{answers: {<name>: {type, choice, confidence, probabilities}}}``。任何失败都返回 None、
不抛异常——决策失败必须退化为普通问答，不能打断主链路。API key 不写日志。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import httpx

from hub.intents import INTENTS, QA_LABEL
from hub.rules import resolve_entity

logger = logging.getLogger(__name__)

SYSTEMONE_PATH = "/compatible-mode/v1/systemone"

# 阶段一判定意图的固定 instructions；criteria 的 key 来自 intents 表 + qa 标签。
INTENT_INSTRUCTIONS = "判断这句话是要求助手执行某个控制动作，还是普通提问"
QA_INSTRUCTIONS = "普通提问、闲聊、创作、翻译，或没有明确控制动作的话"


class DecisionClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 5.0,
    ):
        # 供应商存的 base_url 可能已含 /compatible-mode/v1（如百炼控制台复制的地址），
        # 也可能只是根域名。统一剥掉这段前缀，再拼接完整 systemone 路径，避免重复。
        base = (base_url or "").rstrip("/")
        if base.endswith("/compatible-mode/v1"):
            base = base[: -len("/compatible-mode/v1")]
        self.base_url = base
        self._api_key = api_key
        self.model = model
        self._transport = transport
        self.timeout = timeout

    async def ask(self, content: str, questions: dict) -> dict | None:
        """返回响应里的 answers 字典；失败返回 None。"""
        url = f"{self.base_url}{SYSTEMONE_PATH}"
        payload = {
            "model": self.model,
            "state": {"content": content},
            "questions": questions,
        }
        headers = {
            "authorization": self._api_key,
            "content-type": "application/json",
        }
        started = time.monotonic()
        question_names = list(questions) if isinstance(questions, dict) else []
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "[DECISION] 请求失败 model=%s questions=%s err=%s elapsed=%.0fms",
                self.model, question_names, type(exc).__name__,
                (time.monotonic() - started) * 1000,
            )
            return None
        elapsed = (time.monotonic() - started) * 1000
        if not resp.is_success:
            raw = resp.content.decode("utf-8", errors="replace")
            if self._api_key:
                raw = raw.replace(self._api_key, "***")
            logger.warning(
                "[DECISION] 失败 model=%s questions=%s status=%s elapsed=%.0fms: %s",
                self.model, question_names, resp.status_code, elapsed, raw[:200],
            )
            return None
        try:
            data = json.loads(resp.content)
        except ValueError:
            logger.warning("[DECISION] 非法JSON model=%s status=%s elapsed=%.0fms",
                           self.model, resp.status_code, elapsed)
            return None
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict):
            logger.warning("[DECISION] 缺answers字段 model=%s status=%s elapsed=%.0fms",
                           self.model, resp.status_code, elapsed)
            return None
        logger.info(
            "[DECISION] 成功 model=%s questions=%s status=%s latency_ms=%s elapsed=%.0fms",
            self.model, question_names, resp.status_code,
            data.get("latency_ms", "-") if isinstance(data, dict) else "-", elapsed,
        )
        return answers


def choice_of(answers: dict, name: str) -> tuple[str, float, dict] | None:
    """取 (choice, confidence, probabilities)；字段不合法返回 None，probabilities 缺省 {}。"""
    answer = answers.get(name)
    if not isinstance(answer, dict):
        return None
    choice = answer.get("choice")
    confidence = answer.get("confidence")
    probabilities = answer.get("probabilities")
    if not isinstance(choice, str) or not isinstance(confidence, (int, float)):
        return None
    if not isinstance(probabilities, dict):
        probabilities = {}
    return choice, float(confidence), probabilities


@dataclass
class Decision:
    intent_key: str
    confidence: float
    probabilities: dict = field(default_factory=dict)
    entity: dict | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class DecisionOut:
    """``decide`` 的原始判定带出：即使结论是 qa / 不确定，也保留阶段一判定的原文，
    供上层落库诊断（turns.decision）。"""

    intent: dict | None = None
    entity_failed: bool = False


def _intent_by_key(store, key: str) -> dict | None:
    return store.get_intent(key)


async def decide(
    text: str,
    store,
    client: DecisionClient,
    budget_s: float,
    min_confidence: float,
    out: DecisionOut | None = None,
) -> Decision | None:
    """两阶段判定；返回 None 表示不是命令或不确定（含 qa）。

    ``out`` 可选：传入时，无论返回什么都会写入阶段一的原始判定（``out.intent``），
    并在实体阶段失败时置 ``out.entity_failed = True``。决策模型判成 qa / 低置信 /
    实体解析失败这些情况都通过返回值 None 表达，原始判定信息会随之丢失，
    落库诊断（turns.decision）需要靠 ``out`` 把它带出来。整段共享 budget_s 预算。
    """
    enabled = [i for i in store.list_intents() if i.get("enabled")]
    criteria = {i["key"]: (i.get("instructions") or i["name"]) for i in enabled}
    criteria[QA_LABEL] = QA_INSTRUCTIONS
    questions = {
        "intent": {
            "type": "choice",
            "instructions": INTENT_INSTRUCTIONS,
            "criteria": criteria,
        }
    }
    started = time.monotonic()
    try:
        async with asyncio.timeout(budget_s):
            answers = await client.ask(text, questions)
            if answers is None:
                return None
            picked = choice_of(answers, "intent")
            if picked is None:
                return None
            intent_key, confidence, probabilities = picked
            if out is not None:
                out.intent = {
                    "choice": intent_key,
                    "confidence": confidence,
                    "probabilities": probabilities,
                }
            if intent_key not in criteria:
                return None
            if intent_key == QA_LABEL or confidence < min_confidence:
                return None
            intent = _intent_by_key(store, intent_key)
            if intent is None:
                return None
            entity = None
            raw = {"intent": answers.get("intent", {})}
            if (intent.get("param_kind") == "entity"):
                entity = await _pick_entity(text, store, client)
                if entity is None:
                    if out is not None:
                        out.entity_failed = True
                    return None
                raw["target"] = entity
            return Decision(
                intent_key=intent_key,
                confidence=confidence,
                probabilities=probabilities,
                entity=entity,
                raw=raw,
            )
    except (TimeoutError, asyncio.TimeoutError):
        logger.info("[DECISION] timed out after %.0fms", (time.monotonic() - started) * 1000)
        return None
    finally:
        logger.info(
            "[DECISION] intent=%s conf=%.2f ms=%d",
            intent_key if 'intent_key' in locals() else "-",
            confidence if 'confidence' in locals() else 0.0,
            int((time.monotonic() - started) * 1000),
        )


async def _pick_entity(text: str, store, client: DecisionClient) -> dict | None:
    """阶段二：把当前角色/模型塞进 criteria，选中项必须在原话里出现且能解析。"""
    lower = text.casefold()
    criteria: dict[str, str] = {}
    persona_names = []
    for p in store.list_personas():
        for n in _names(p):
            persona_names.append(n)
            criteria[n] = "角色/音色"
    provider_names = []
    for p in store.list_providers():
        for n in _names(p):
            label = f"模型:{n}" if n in criteria else n
            criteria[label] = "回答模型"
            provider_names.append((label, n))
    if not criteria:
        return None
    answers = await client.ask(text, {
        "target": {"type": "choice", "instructions": "这句话要切换成哪个角色或模型？",
                   "criteria": criteria},
    })
    if answers is None:
        return None
    picked = choice_of(answers, "target")
    if picked is None:
        return None
    label, _confidence, _prob = picked
    # label 可能是「模型:名字」前缀；还原真实名字做子串与实体解析。
    actual = label
    for label_key, real_name in provider_names:
        if label == label_key:
            actual = real_name
            break
    if actual.casefold() not in lower:
        return None  # 幻觉：选中的实体名根本不在原话里
    return resolve_entity(store, actual)


def _names(item: dict) -> list[str]:
    names = [item.get("name") or ""]
    names += (item.get("aliases") or "").replace("，", ",").split(",")
    return [n.strip().casefold() for n in names if n.strip()]
