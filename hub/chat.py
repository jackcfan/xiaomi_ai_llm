"""对话服务：语音命令 / 上下文 / 联网检索 / LLM→分句→TTS 流式输出 PCM。

``answer`` 与 ``speak`` 只产出 PCM（不含 WAV 头）。首个 PCM 产出前的任何失败都直接抛出，
由路由层转成 HTTP 502；音箱中途断开（生成器被关闭/取消）时保存已生成的部分回答并标记
``interrupted=1``，同时显式关闭底层模型流，停止模型生成。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

import anyio

from hub.context import select_context
from hub.intents import INTENT_KEYS, execute as execute_intent
from hub.llm import ChatClient
from hub.rules import load_ruleset, normalize
from hub.store import Store
from hub.web import ground
from server.streaming_pipeline import StreamingPipeline
from hub.tts import edge_tts_factory, siliconflow_tts_factory

logger = logging.getLogger(__name__)

SAMPLE_RATE = 32000
FILLER_TEXT = "搜索中，请稍等。"
APOLOGY_TEXT = "抱歉，暂时无法回答。"
DEFAULT_GAP_SECONDS = 300
DEFAULT_MAX_TURNS = 10
# TTS 引擎关键字（与 store.TTS_PROVIDERS 对应）；CosyVoice2 的 key 存 providers 表，
# 按名/别名匹配不到时回退给当前 LLM provider（通常是 DeepSeek，其 key 是 OpenAI 兼容的）。
SILICONFLOW_ENGINE = "siliconflow"
# server/streaming_pipeline.StreamingPipeline.process 产出的 error 事件内容前缀：
# 模型流失败为 "LLM: ..."，单句 TTS 失败为 "TTS: ..."。那里的文案改动时需同步这里。
PIPELINE_LLM_ERROR = "LLM:"
PIPELINE_TTS_ERROR = "TTS:"


@dataclass
class Route:
    """一次请求的路由决策结果：识别来源（rule/model/none）与可执行参数。"""

    route: str = "none"
    intent_key: str = ""
    rule_id: int | None = None
    params: dict = field(default_factory=dict)
    decision: dict = field(default_factory=dict)


class ChatError(RuntimeError):
    """首个 PCM 之前的失败（路由层返回 502）。"""


def _extra_body(provider: dict) -> dict | None:
    """供应商的附加请求体（JSON 对象文本）；空或非法时忽略（后台保存时已校验）。"""
    raw = (provider.get("extra_body") or "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        logger.warning("[CHAT] provider %s: invalid extra_body ignored", provider.get("name"))
        return None
    return value if isinstance(value, dict) else None


def _int_setting(value: str | None, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _float_setting(value: str | None, default: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


_THINK_START = re.compile(r"<\s*think\s*>", re.IGNORECASE)
_THINK_END = re.compile(r"<\s*/\s*think\s*>", re.IGNORECASE)


def _strip_think_tokens(tokens) -> str:
    """拼接模型 token 流并去掉 <think>…</think> 思考段。

    非流式场景下全文已齐，用正则切掉思考段即可；未闭合的 <think> 连后面一起丢弃，
    与 StreamingPipeline 流结束时的行为一致（思考段未结束则剩余缓冲不输出）。
    """
    text = "".join(tokens)
    out: list[str] = []
    i = 0
    while i < len(text):
        start = _THINK_START.search(text, i)
        if start is None:
            out.append(text[i:])
            break
        out.append(text[i:start.start()])
        end = _THINK_END.search(text, start.end())
        if end is None:
            break  # 未闭合的思考段：丢弃其后全部内容
        i = end.end()
    return "".join(out)


class _TrackedLLM:
    """包装 LLM 客户端，记下 chat_stream 生成器以便断开时显式 aclose。

    StreamingPipeline.process 从不关闭模型生成器；关闭 process 本身不会立刻停止 HTTP 流。
    """

    def __init__(self, client):
        self.client = client
        self.stream = None

    def chat_stream(self, **kwargs):
        self.stream = self.client.chat_stream(**kwargs)
        return self.stream


class ChatService:
    def __init__(
        self,
        store: Store,
        llm_factory=ChatClient,
        tts_factory=edge_tts_factory,
        web_ground=ground,
        clock=time.time,
        decision=None,
    ):
        self.store = store
        self.llm_factory = llm_factory
        self.tts_factory = tts_factory
        self.web_ground = web_ground
        self.clock = clock
        self.decision = decision  # 可注入；生产默认 hub.decision.decide（Task 7 起使用）
        self._pending: tuple[float, Route] | None = None  # (到期时间戳, 待确认 route)
        self._last_command: tuple[float, str, dict, int] | None = None
        # 垫话 PCM 缓存：(engine, voice, speed, volume) -> PCM 块列表，避免每次搜索重复合成。
        self._filler_cache: dict[tuple, list[bytes]] = {}

    # --- helpers ---------------------------------------------------------

    def _current(self) -> tuple[dict, dict]:
        provider = self.store.current_provider()
        persona = self.store.current_persona()
        if provider is None:
            raise ChatError("no LLM provider configured")
        if persona is None:
            raise ChatError("no persona configured")
        return provider, persona

    def _search_provider(self, fallback: dict) -> dict:
        value = self.store.get_setting("search_provider_id")
        if value is not None and str(value).isdecimal():
            provider = self.store.get_provider(int(value))
            if provider is not None:
                return provider
        return fallback

    def _make_tts(self, persona: dict):
        """按 persona 的 TTS 提供方构造客户端。

        siliconflow（CosyVoice2）的 api_key 从 providers 表里按名/别名匹配
        （key 存 providers，见需求）；模型固定为 CosyVoice2，不读 provider.model
        （那是 LLM 模型名）。匹配不到直接报错，绝不拿其它厂商的 key 发往硅基流动。
        """
        engine = self.store.persona_tts_engine(persona)
        voice = persona.get("voice") or ""
        if engine == SILICONFLOW_ENGINE:
            tts_provider = self.store.find_tts_provider(engine)
            if tts_provider is None:
                raise ChatError(
                    "no siliconflow provider configured：请在 /admin/providers 添加"
                    "「硅基流动」供应商（名称或别名含 siliconflow）并填 API key"
                )
            return siliconflow_tts_factory(
                voice,
                api_key=(tts_provider or {}).get("api_key") or "",
                base_url=(tts_provider or {}).get("base_url") or "",
            )
        return self.tts_factory(voice)

    def _make_llm(self, provider: dict):
        return self.llm_factory(
            base_url=provider["base_url"],
            api_key=provider["api_key"],
            model=provider["model"],
            temperature=provider.get("temperature"),
            max_tokens=provider.get("max_tokens"),
            extra_body=_extra_body(provider),
        )

    def _system_prompt(self, persona: dict) -> str:
        base = self.store.get_setting("base_prompt") or ""
        return base + "\n" + (persona.get("prompt") or "")

    def _context(self) -> list[dict]:
        gap = _int_setting(self.store.get_setting("context_gap_seconds"), DEFAULT_GAP_SECONDS)
        max_turns = _int_setting(self.store.get_setting("context_max_turns"), DEFAULT_MAX_TURNS)
        turns = self.store.recent_turns(max_turns * 2 + 5)
        messages = select_context(turns, self.clock(), gap, max_turns)
        # 被打断的回答可能是空串；空消息不发给模型。
        return [m for m in messages if m.get("content")]

    async def _tts_pcm(self, tts, text: str, speed: float, volume: float) -> AsyncIterator[bytes]:
        """纯 TTS：沿用 server/main.py stream_tts 的分句与校验，任何错误都抛出。"""
        pipeline = StreamingPipeline(
            None, tts, sample_rate=SAMPLE_RATE, speed=speed, volume=volume
        )
        for sentence in pipeline._split_sync(text):
            has_audio = False
            async with contextlib.aclosing(pipeline._tts_stream(sentence)) as chunks:
                async for chunk in chunks:
                    if chunk["type"] == "error":
                        raise ChatError("TTS sentence synthesis failed")
                    if chunk["type"] == "audio":
                        pcm = chunk["data"]
                        if not pcm or len(pcm) % 2:
                            raise ChatError("TTS returned invalid PCM")
                        has_audio = True
                        yield pcm
            if not has_audio:
                raise ChatError("TTS returned no audible PCM")

    async def _filler_pcm(self, tts, speed: float, volume: float) -> list[bytes]:
        """垫话 PCM（按音色/语速/音量缓存）；尽力而为，失败返回空列表并记日志。"""
        key = (getattr(tts, "voice", ""), speed, volume)
        cached = self._filler_cache.get(key)
        if cached is not None:
            return cached
        chunks: list[bytes] = []
        try:
            async with contextlib.aclosing(self._tts_pcm(tts, FILLER_TEXT, speed, volume)) as audio:
                async for pcm in audio:
                    chunks.append(pcm)
        except Exception as exc:
            logger.warning("[CHAT] filler TTS failed, skipped: %s", type(exc).__name__)
            return []
        self._filler_cache[key] = chunks
        return chunks

    # --- public ----------------------------------------------------------

    async def speak(self, text: str, speed: float, volume: float) -> AsyncIterator[bytes]:
        persona = self.store.current_persona()
        if persona is None:
            raise ChatError("no persona configured")
        tts = self._make_tts(persona)
        async with contextlib.aclosing(self._tts_pcm(tts, text, speed, volume)) as audio:
            async for pcm in audio:
                yield pcm

    async def answer(self, text: str, speed: float, volume: float) -> AsyncIterator[bytes]:
        norm = normalize(text)
        # 待确认命令的后续确认（「对/是/嗯」），30 秒内有效，hub 全局。
        pending = self._take_pending(norm)
        if pending is not None:
            audio, _turn_id = await self._run_intent(pending, text, speed, volume)
            async with contextlib.aclosing(audio) as agen:
                async for pcm in agen:
                    yield pcm
            return
        # 纠正检测先于路由：上一条命令可能被「不对/不是/我说的是/搞错」纠正，
        # 而纠正短语本身往往又被判成命令（如「不对，是温柔」→ switch_target）。
        self._maybe_mark_correction(norm)
        route = await self._route(text)
        if route.intent_key and route.intent_key != "qa":
            # 低置信确认带：模型拿不准时不执行，播确认、记 _pending。
            confirm = self._needs_confirm(route)
            if confirm:
                inner = self._confirm_ask(route, text, speed, volume)
            else:
                inner, turn_id = await self._run_intent(route, text, speed, volume)
                if turn_id is not None:
                    self._last_command = (self.clock(), route.intent_key,
                                          (route.params.get("entity") or {}).get("name"), turn_id)
                self._maybe_recheck(norm, route)  # 规则命中后抽样复核
        else:
            provider, persona = self._current()
            inner = self._qa(text, provider, persona, speed, volume, route=route)
        # aclosing：answer 被关闭时立即关闭内层生成器，使其 finally（落库、关模型流）
        # 同步执行完，而不是留给 asyncio 的 asyncgen 终结器。
        async with contextlib.aclosing(inner) as audio:
            async for pcm in audio:
                yield pcm

    async def answer_text(self, text: str) -> str:
        """文本聊天（/admin/history 测试入口用）：与 answer 相同的路由/意图，但不合成音频。"""
        norm = normalize(text)
        pending = self._take_pending(norm)
        if pending is not None:
            reply, _turn_id = await self._run_intent_text(pending, text)
            return reply
        self._maybe_mark_correction(norm)
        route = await self._route(text)
        if route.intent_key and route.intent_key != "qa":
            if self._needs_confirm(route):
                return self._confirm_ask_text(route, text)
            reply, turn_id = await self._run_intent_text(route, text)
            if turn_id is not None:
                self._last_command = (self.clock(), route.intent_key,
                                      (route.params.get("entity") or {}).get("name"), turn_id)
            return reply
        provider, persona = self._current()
        return await self._qa_text(text, provider, persona, route)

    def _take_pending(self, norm: str) -> Route | None:
        if self._pending is None:
            return None
        expires, route = self._pending
        self._pending = None
        if norm not in ("对", "是", "嗯", "好", "可以", "没错"):
            return None
        if self.clock() > expires:
            return None
        return route

    def _needs_confirm(self, route: Route) -> bool:
        if not _int_setting(self.store.get_setting("decision_confirm_band"), 1):
            return False
        if not route.params.get("entity"):
            return False  # 确认带只适用于有实体的意图（切换），避免「你是要切换到吗」
        conf = float(route.decision.get("confidence") or 0.0)
        min_conf = _float_setting(self.store.get_setting("decision_min_confidence"), 0.6)
        return route.route == "model" and min_conf <= conf < 0.75

    def _confirm_ask(self, route: Route, text, speed, volume) -> AsyncIterator[bytes]:
        name = (route.params.get("entity") or {}).get("name", "")
        reply = f"你是要切换到{name}吗"
        provider, persona = self._current()
        self._pending = (self.clock() + 30, route)
        self.store.add_turn(
            text, reply, provider["name"], persona["name"], "command",
            route=route.route, intent_key=route.intent_key, rule_id=route.rule_id,
            decision=json.dumps(route.decision, ensure_ascii=False) if route.decision else "",
            learn_state="new", ts=self.clock(),
        )
        tts = self._make_tts(persona)
        return self._tts_pcm(tts, reply, speed, volume)

    def _confirm_ask_text(self, route: Route, text: str) -> str:
        name = (route.params.get("entity") or {}).get("name", "")
        reply = f"你是要切换到{name}吗"
        provider, persona = self._current()
        self._pending = (self.clock() + 30, route)
        self.store.add_turn(
            text, reply, provider["name"], persona["name"], "command",
            route=route.route, intent_key=route.intent_key, rule_id=route.rule_id,
            decision=json.dumps(route.decision, ensure_ascii=False) if route.decision else "",
            learn_state="new", ts=self.clock(),
        )
        return reply

    def _maybe_mark_correction(self, norm: str) -> None:
        """纠正检测：上一条命令后 60 秒内，若下一条话匹配纠正词，把上一条标记 corrected。"""
        if self._last_command is None:
            return
        ts, intent_key, entity_name, turn_id = self._last_command
        self._last_command = None
        if self.clock() - ts > 60 or turn_id == -1:
            return
        if norm.startswith(("不对", "不是", "我说的是", "搞错")):
            self.store.set_turn_corrected(turn_id)
            logger.info("[CHAT] correction detected for turn %s", turn_id)

    def _maybe_recheck(self, norm: str, route: Route) -> "asyncio.Task | None":
        """规则命中后抽样调决策模型复核；返回启动的任务（fire-and-forget），不一致累加 mismatch。"""
        if route.route != "rule":
            return None
        rule = self.store.get_rule(route.rule_id)
        if rule is None:
            return None
        ratio = _float_setting(self.store.get_setting("decision_recheck_ratio"), 0.05)
        if rule.get("source") == "learned" and (rule.get("hit_count") or 0) < 50:
            ratio = 0.2
        if random.random() >= ratio:
            return None
        client = self._decision_client()
        if client is None:
            return None

        async def do_recheck():
            try:
                from hub.decision import decide
                d = await decide(
                    norm, self.store, client,
                    _float_setting(self.store.get_setting("decision_timeout_ms"), 800) / 1000,
                    _float_setting(self.store.get_setting("decision_min_confidence"), 0.6),
                )
                got = d.intent_key if d is not None else "qa"
                mismatch = got != route.intent_key
                self.store.bump_rule_stats(route.rule_id, recheck=True, mismatch=mismatch)
                if mismatch:
                    self._maybe_disable_rule(route.rule_id)
            except Exception as exc:
                logger.warning("[CHAT] recheck failed: %s", type(exc).__name__)

        return asyncio.ensure_future(do_recheck())

    def _maybe_disable_rule(self, rule_id: int) -> bool:
        """复核不一致率达到阈值时停用规则；返回是否停用。"""
        row = self.store.get_rule(rule_id)
        if not row:
            return False
        recheck = row.get("recheck_count") or 0
        mismatch = row.get("mismatch_count") or 0
        if recheck >= 10 and mismatch / recheck >= 0.25:
            self.store.upsert_rule({"id": rule_id, "status": "disabled"})
            logger.warning("[CHAT] rule %s auto-disabled after recheck mismatches", rule_id)
            return True
        return False

    async def _route(self, text: str) -> Route:
        """三级路由：正则表（rule）→ 决策模型（model）→ 未命中（none）。

        model 表示决策模型确实被调用过（结论可能是命令或 qa）；none 表示没调
        （正则未命中且被闸门/开关拦下、或决策异常）。intent_key == qa 表示模型判成问答。
        """
        norm = normalize(text)
        hit = load_ruleset(self.store).match(norm)
        if hit is not None:
            self.store.bump_rule_stats(hit.rule.id, hit=True)
            return Route(
                route="rule",
                intent_key=hit.rule.intent_key,
                rule_id=hit.rule.id,
                params={"entity": hit.entity} if hit.entity else {},
            )

        if not norm:  # 只有标点/只有唤醒词：不走决策模型，直接问答
            return Route()

        if not _int_setting(self.store.get_setting("decision_enabled"), 1):
            return Route()
        gate_len = _int_setting(self.store.get_setting("decision_gate_len"), 40)
        if len(norm) > gate_len:
            return Route()

        if self.decision is None:
            from hub.decision import decide as _decide
            self.decision = _decide

        client = self._decision_client()
        if client is None:
            logger.warning("[CHAT] decision provider not configured, skip decision")
            return Route()

        budget_s = _float_setting(self.store.get_setting("decision_timeout_ms"), 800) / 1000
        min_conf = _float_setting(self.store.get_setting("decision_min_confidence"), 0.6)
        from hub.decision import DecisionOut

        out = DecisionOut()
        try:
            decision = await self.decision(norm, self.store, client, budget_s, min_conf, out)
        except Exception as exc:  # 决策异常绝不打断主链路
            logger.warning("[CHAT] decision failed: %s", type(exc).__name__)
            return Route()

        if decision is None:
            # 模型调过、判成 qa 或不确定：intent_key 记 qa，学习任务据此取负例。
            # 阶段一原始判定带进 decision，落库后便于诊断「为什么判成 qa」。
            raw = out.intent or {}
            if out.entity_failed:
                raw = {**raw, "entity_failed": True}
            return Route(route="model", intent_key="qa", decision=raw)
        return Route(
            route="model",
            intent_key=decision.intent_key,
            params={"entity": decision.entity} if decision.entity else {},
            decision={
                "confidence": decision.confidence,
                "probabilities": decision.probabilities,
                **(out.intent or {}),
                **({"entity_failed": True} if out.entity_failed else {}),
            },
        )

    def _decision_client(self):
        provider_id = self.store.get_setting("decision_provider_id")
        if provider_id and provider_id.isdecimal():
            provider = self.store.get_provider(int(provider_id))
            if provider and (provider.get("base_url") or "").strip():
                from hub.decision import DecisionClient
                return DecisionClient(
                    base_url=provider["base_url"],
                    api_key=provider.get("api_key") or "",
                    model=provider.get("model") or "decision-model-preview",
                )
        return None

    async def _run_intent(self, route: Route, text, speed, volume):
        if route.intent_key not in INTENT_KEYS:
            # 规则/意图表里的未知 key 绝不执行（spec §5.1）；当作未命中回落问答。
            logger.warning("[CHAT] unknown intent key %s ignored", route.intent_key)
            provider, persona = self._current()
            audio = self._qa(text, provider, persona, speed, volume, route=route)
            return audio, None
        entity = route.params.get("entity")
        reply, kind = execute_intent(self.store, route.intent_key, entity or {})
        provider, persona = self._current()
        logger.info("[CMD] %s -> %s", route.intent_key, (entity or {}).get("name") or "-")
        turn_id = self.store.add_turn(
            text, reply, provider["name"], persona["name"], kind,
            route=route.route, intent_key=route.intent_key, rule_id=route.rule_id,
            decision=json.dumps(route.decision, ensure_ascii=False) if route.decision else "",
            learn_state="new", ts=self.clock(),
        )
        tts = self._make_tts(persona)
        audio = self._tts_pcm(tts, reply, speed, volume)
        return audio, turn_id

    async def _run_intent_text(self, route: Route, text: str) -> tuple[str, int | None]:
        if route.intent_key not in INTENT_KEYS:
            logger.warning("[CHAT] unknown intent key %s ignored", route.intent_key)
            provider, persona = self._current()
            return await self._qa_text(text, provider, persona, route), None
        entity = route.params.get("entity")
        reply, kind = execute_intent(self.store, route.intent_key, entity or {})
        provider, persona = self._current()
        logger.info("[CMD] %s -> %s", route.intent_key, (entity or {}).get("name") or "-")
        turn_id = self.store.add_turn(
            text, reply, provider["name"], persona["name"], kind,
            route=route.route, intent_key=route.intent_key, rule_id=route.rule_id,
            decision=json.dumps(route.decision, ensure_ascii=False) if route.decision else "",
            learn_state="new", ts=self.clock(),
        )
        return reply, turn_id

    async def _qa(self, text, provider, persona, speed, volume, route: Route | None = None) -> AsyncIterator[bytes]:
        route = route or Route()
        context = self._context()
        tts = self._make_tts(persona)
        audio_started = False  # 已向调用方产出过任何 PCM（含垫话）
        answer_audio = False   # 模型回答本身已产出 PCM
        parts: list[str] = []
        sources = ""
        saved = False
        interrupted = False
        truncated = False  # 模型在回答中途报错
        llm = _TrackedLLM(self._make_llm(provider))
        events = None
        ground_task = None
        tool_wait = None

        def save(answer_text: str, was_interrupted: bool) -> None:
            nonlocal saved
            self.store.add_turn(
                text, answer_text, provider["name"], persona["name"], "qa",
                web_sources=sources, interrupted=was_interrupted, ts=self.clock(),
                route=route.route, intent_key=route.intent_key, rule_id=route.rule_id,
                decision=json.dumps(route.decision, ensure_ascii=False) if route.decision else "",
                learn_state="skipped" if route.route == "none" else "new",
            )
            saved = True

        try:
            # 联网规划与检索在后台进行：规划一决定 search/fetch（工具请求之前）就先播垫话，
            # 与设备端时机一致；垫话播放期间检索继续进行。
            tool_started = asyncio.Event()
            ground_task = asyncio.ensure_future(self.web_ground(
                text, context, self._search_provider(provider), on_tool_start=tool_started.set,
            ))
            tool_wait = asyncio.ensure_future(tool_started.wait())
            await asyncio.wait({ground_task, tool_wait}, return_when=asyncio.FIRST_COMPLETED)
            if tool_started.is_set():
                for pcm in await self._filler_pcm(tts, speed, volume):
                    audio_started = True
                    yield pcm
            web = await ground_task
            sources = "\n".join(web.sources or [])

            if web.action in ("search", "fetch") and len(web.evidence_messages) >= 2:
                # 证据紧跟问题：system, context..., user(问题), system(证据提示), user(外部参考资料)。
                history = context + [{"role": "user", "content": text}, web.evidence_messages[0]]
                message = web.evidence_messages[1]["content"]
            else:
                history = context
                message = text

            pipeline = StreamingPipeline(
                llm, tts, sample_rate=SAMPLE_RATE, speed=speed, volume=volume
            )
            events = pipeline.process(message, self._system_prompt(persona), history)
            async for event in events:
                kind = event.get("type")
                if kind == "text":
                    parts.append(event.get("content") or "")
                elif kind == "audio":
                    pcm = event.get("data") or b""
                    if not pcm:
                        continue
                    audio_started = True
                    answer_audio = True
                    yield pcm
                elif kind == "error":
                    detail = str(event.get("content") or "")
                    if not audio_started:
                        raise ChatError(f"answer failed before audio: {detail[:200]}")
                    logger.warning("[CHAT] error after audio started: %s", detail[:200])
                    if detail.startswith(PIPELINE_TTS_ERROR):
                        continue  # 单句 TTS 失败：流水线继续合成后续句子
                    # PIPELINE_LLM_ERROR（或未知错误）：模型流中途失败，process 随即结束。
                    if answer_audio:
                        truncated = True
            if not answer_audio:
                if not audio_started:
                    raise ChatError("model produced no answer")
                # 垫话已播出（HTTP 200 已提交）：记录失败，并尽力播一句道歉，避免静默。
                logger.warning("[CHAT] model produced no answer after filler")
                save("", True)
                try:
                    async with contextlib.aclosing(
                        self._tts_pcm(tts, APOLOGY_TEXT, speed, volume)
                    ) as audio:
                        async for pcm in audio:
                            yield pcm
                except Exception as exc:
                    logger.warning("[CHAT] apology TTS failed: %s", type(exc).__name__)
                return
            save("".join(parts), truncated)
        except (GeneratorExit, asyncio.CancelledError):
            interrupted = True
            raise
        finally:
            if interrupted and not saved:
                # 先同步落库，再关闭流（关闭可能需要等待网络）。
                save("".join(parts), True)
                logger.info("[CHAT] interrupted, saved %d chars", len("".join(parts)))
            with anyio.CancelScope(shield=True):
                for task in (tool_wait, ground_task):
                    if task is not None and not task.done():
                        task.cancel()
                        with contextlib.suppress(BaseException):
                            await task
                if events is not None:
                    await events.aclose()
                if llm.stream is not None:
                    await llm.stream.aclose()

    async def _qa_text(self, text: str, provider: dict, persona: dict, route: Route | None = None) -> str:
        """纯文本 QA：与 _qa 相同的联网检索与 LLM 调用，但不合成音频，返回全文。"""
        route = route or Route()
        context = self._context()
        sources = ""
        llm = _TrackedLLM(self._make_llm(provider))
        ground_task = None
        tool_wait = None
        try:
            tool_started = asyncio.Event()
            ground_task = asyncio.ensure_future(self.web_ground(
                text, context, self._search_provider(provider), on_tool_start=tool_started.set,
            ))
            tool_wait = asyncio.ensure_future(tool_started.wait())
            await asyncio.wait({ground_task, tool_wait}, return_when=asyncio.FIRST_COMPLETED)
            web = await ground_task
            sources = "\n".join(web.sources or [])
            if web.action in ("search", "fetch") and len(web.evidence_messages) >= 2:
                history = context + [{"role": "user", "content": text}, web.evidence_messages[0]]
                message = web.evidence_messages[1]["content"]
            else:
                history = context
                message = text
            tokens = []
            async for token in llm.chat_stream(
                message=message,
                system_prompt=self._system_prompt(persona),
                conversation_history=history,
            ):
                tokens.append(token)
            answer = _strip_think_tokens(tokens)
            self.store.add_turn(
                text, answer, provider["name"], persona["name"], "qa",
                web_sources=sources, interrupted=False, ts=self.clock(),
                route=route.route, intent_key=route.intent_key, rule_id=route.rule_id,
                decision=json.dumps(route.decision, ensure_ascii=False) if route.decision else "",
                learn_state="skipped" if route.route == "none" else "new",
            )
            return answer
        finally:
            with anyio.CancelScope(shield=True):
                for task in (tool_wait, ground_task):
                    if task is not None and not task.done():
                        task.cancel()
                        with contextlib.suppress(BaseException):
                            await task
                if llm.stream is not None:
                    await llm.stream.aclose()
