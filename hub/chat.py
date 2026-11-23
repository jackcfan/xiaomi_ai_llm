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
import time
from typing import AsyncIterator

import anyio

from hub.commands import parse_command
from hub.context import select_context
from hub.llm import ChatClient
from hub.store import Store
from hub.web import ground
from server.streaming_pipeline import StreamingPipeline
from hub.tts import edge_tts_factory

logger = logging.getLogger(__name__)

SAMPLE_RATE = 32000
FILLER_TEXT = "搜索中，请稍等。"
APOLOGY_TEXT = "抱歉，暂时无法回答。"
DEFAULT_GAP_SECONDS = 300
DEFAULT_MAX_TURNS = 10
# server/streaming_pipeline.StreamingPipeline.process 产出的 error 事件内容前缀：
# 模型流失败为 "LLM: ..."，单句 TTS 失败为 "TTS: ..."。那里的文案改动时需同步这里。
PIPELINE_LLM_ERROR = "LLM:"
PIPELINE_TTS_ERROR = "TTS:"


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
    ):
        self.store = store
        self.llm_factory = llm_factory
        self.tts_factory = tts_factory
        self.web_ground = web_ground
        self.clock = clock
        # 垫话 PCM 缓存：(voice, speed, volume) -> PCM 块列表，避免每次搜索重复合成。
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

    async def _tts_pcm(self, voice: str, text: str, speed: float, volume: float) -> AsyncIterator[bytes]:
        """纯 TTS：沿用 server/main.py stream_tts 的分句与校验，任何错误都抛出。"""
        pipeline = StreamingPipeline(
            None, self.tts_factory(voice), sample_rate=SAMPLE_RATE, speed=speed, volume=volume
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

    async def _filler_pcm(self, voice: str, speed: float, volume: float) -> list[bytes]:
        """垫话 PCM（按音色/语速/音量缓存）；尽力而为，失败返回空列表并记日志。"""
        key = (voice, speed, volume)
        cached = self._filler_cache.get(key)
        if cached is not None:
            return cached
        chunks: list[bytes] = []
        try:
            async with contextlib.aclosing(self._tts_pcm(voice, FILLER_TEXT, speed, volume)) as audio:
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
        async with contextlib.aclosing(self._tts_pcm(persona["voice"], text, speed, volume)) as audio:
            async for pcm in audio:
                yield pcm

    async def answer(self, text: str, speed: float, volume: float) -> AsyncIterator[bytes]:
        provider, persona = self._current()
        command = parse_command(text, self.store.list_personas(), self.store.list_providers())
        if command is not None:
            inner = self._run_command(text, command, speed, volume)
        else:
            inner = self._qa(text, provider, persona, speed, volume)
        # aclosing：answer 被关闭时立即关闭内层生成器，使其 finally（落库、关模型流）
        # 同步执行完，而不是留给 asyncio 的 asyncgen 终结器。
        async with contextlib.aclosing(inner) as audio:
            async for pcm in audio:
                yield pcm

    async def _run_command(self, text, command, speed, volume) -> AsyncIterator[bytes]:
        if command.kind == "persona":
            self.store.set_setting("current_persona_id", str(command.target_id))
            reply, kind = f"好的，已切换为{command.target_name}", "command"
        elif command.kind == "provider":
            self.store.set_setting("current_provider_id", str(command.target_id))
            reply, kind = f"好的，已切换到{command.target_name}", "command"
        else:
            reply, kind = "好的，我们重新开始", "reset"
        provider, persona = self._current()  # 切换后的音色/名称
        logger.info("[CMD] %s -> %s", command.kind, command.target_name or "-")
        self.store.add_turn(
            text, reply, provider["name"], persona["name"], kind, ts=self.clock()
        )
        async with contextlib.aclosing(self._tts_pcm(persona["voice"], reply, speed, volume)) as audio:
            async for pcm in audio:
                yield pcm

    async def _qa(self, text, provider, persona, speed, volume) -> AsyncIterator[bytes]:
        context = self._context()
        voice = persona["voice"]
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
                for pcm in await self._filler_pcm(voice, speed, volume):
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
                llm, self.tts_factory(voice), sample_rate=SAMPLE_RATE, speed=speed, volume=volume
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
                        self._tts_pcm(voice, APOLOGY_TEXT, speed, volume)
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
