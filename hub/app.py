"""hub 的 FastAPI 应用：设备接口（与 server/main.py 同格式的流式 WAV）。

启动：``python -m hub``（会先检查 ffmpeg 并把 stdout 改成 UTF-8，再以 uvicorn 加载 ``hub.app:app``）。
模块级 ``app`` 只在首次访问时创建，导入本模块（测试）不会建库。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import struct
import time
from typing import AsyncIterator

import anyio
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from hub.admin import make_admin_router
from hub.chat import SAMPLE_RATE, ChatService
from hub.store import Store

logger = logging.getLogger(__name__)

MAX_DATA = 180 * SAMPLE_RATE * 2
# 流式响应期间轮询客户端断开的间隔（秒）：越小越早停模型/TTS，越大越省 CPU。
DISCONNECT_POLL_SECONDS = 0.25


def wav_header() -> bytes:
    """与 server/main.py 相同的流式 RIFF 头（32 kHz/16-bit/单声道，data 长度取上限）。"""
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + MAX_DATA, b"WAVE", b"fmt ",
        16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16,
        b"data", MAX_DATA,
    )


async def _close(gen) -> None:
    # 断开时任务已被取消；屏蔽取消，确保生成器的 finally（存历史、关模型流）跑完。
    if gen.ag_running:
        # 不该发生：__anext__ 都在 _next_pcm 里取消并等到收尾（见 _cancel_and_drain），
        # 走到这里时生成器不可能还在运行。真出现时 aclose() 只会抛 RuntimeError 并掩盖
        # 真正的清理逻辑，记一条错误日志即可。
        logger.error("audio generator still running, aclose() skipped")
        return
    with anyio.CancelScope(shield=True):
        await gen.aclose()


async def _cancel_and_drain(task: asyncio.Task) -> None:
    """取消 task 并等它收尾：不把它的取消/异常抛给调用方，也不遗留未 await 的任务。

    被取消的可能是 ``audio.__anext__``：``chat._qa`` 的中断落库（interrupted=1）与关模型流
    都在它自己的 finally 里跑，必须等它跑完，否则模型与 TTS 会继续到回答结束。
    """
    task.cancel()
    with anyio.CancelScope(shield=True):
        while not task.done():
            with contextlib.suppress(BaseException):
                # 用 wait 而不是 await task：await 一个已取消的 task 会抛 CancelledError，
                # 与"外层取消"难以区分；wait 只等完成，不传播子任务结果。
                await asyncio.wait({task})


class ClientGone(Exception):
    """客户端在看响应时断开（音频生成器已按中断路径落库并关流）。"""


class _DisconnectWatcher:
    """轮询 ASGI 断开信号（``http.disconnect``），供流式响应期间使用。

    不依赖 Starlette 的 StreamingResponse 是否把断开传到生成器：uvicorn 在客户端离开后
    ``send()`` 直接丢弃写入（不报错），只有主动 ``receive()`` 才看得到断开。
    """

    def __init__(self, request: Request, interval: float = DISCONNECT_POLL_SECONDS):
        self.request = request
        self.interval = interval
        self.gone = asyncio.Event()
        self.task = asyncio.ensure_future(self._poll())
        self.task.set_name("hub-disconnect-poll")

    async def _poll(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval)
                if await self.request.is_disconnected():
                    self.gone.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # 请求生命周期已结束（receive 不再可用）：无需继续轮询。
            return

    async def stop(self) -> None:
        # 取消后必须 await：否则留下一个"已取消但从未收回"的轮询任务。
        await _cancel_and_drain(self.task)


async def _next_pcm(audio: AsyncIterator[bytes], watcher: _DisconnectWatcher) -> bytes:
    """取下一块 PCM；期间客户端断开就取消这次 ``__anext__`` 并抛 ClientGone。

    取消会以 CancelledError 送进 hub.chat 的生成器，触发其 finally：保存已生成的部分回答
    （interrupted=1）并显式关闭模型流，而不是继续生成、合成到回答结束。

    Starlette 在 FIN 时还会直接取消响应任务（``listen_for_disconnect`` → 任务组 cancel），
    取消点就落在这里的 ``asyncio.wait`` 上。那种情况下同样要先把 ``__anext__`` 取消并等它
    收尾：丢下它会让生成器永远停在 yield 上，既不落库也不关模型流。
    """
    if watcher.gone.is_set():
        raise ClientGone  # 已断开：连下一块都不再向模型/ TTS 要
    current = asyncio.current_task()
    cancels_before = current.cancelling() if current is not None else 0
    getter = asyncio.ensure_future(audio.__anext__())
    getter.set_name("hub-pcm-next")
    waiter = asyncio.ensure_future(watcher.gone.wait())
    waiter.set_name("hub-pcm-disconnect")
    try:
        try:
            await asyncio.wait({getter, waiter}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            # 外层取消（通常是 Starlette 收到 FIN）：先让 chat._qa 走完中断落库/关流再抛。
            await _cancel_and_drain(getter)
            raise
        if getter.done():
            return getter.result()
        await _cancel_and_drain(getter)
        if current is not None and current.cancelling() > cancels_before:
            # 收尾期间外层也取消了：如实往上抛，不要降级成 ClientGone（那会吞掉取消）。
            raise asyncio.CancelledError
        raise ClientGone
    finally:
        await _cancel_and_drain(waiter)


async def stream_wav(
    request: Request, audio: AsyncIterator[bytes], label: str
) -> Response:
    """预取首块 PCM：失败返回 502，成功后才提交 200 与 WAV 头。

    预取与后续流式输出都与断开轮询并行（首块可能要等模型/首句 TTS 数秒），客户端离开时
    立刻停掉模型调用，而不是等 Starlette/uvicorn 把断开传到生成器。
    """
    t0 = time.time()
    watcher = _DisconnectWatcher(request)
    try:
        first = await _next_pcm(audio, watcher)
    except StopAsyncIteration as error:
        await watcher.stop()
        await _close(audio)
        logger.warning("[%s] no audio produced", label)
        raise HTTPException(status_code=502, detail=f"{label} produced no audio") from error
    except ClientGone:
        await watcher.stop()
        await _close(audio)
        logger.info("[%s] client gone before audio", label)
        return Response(content=b"", media_type="audio/wav")
    except Exception as error:
        await watcher.stop()
        await _close(audio)
        logger.warning("[%s] failed before audio: %s", label, type(error).__name__)
        raise HTTPException(status_code=502, detail=f"{label} failed") from error
    except BaseException:
        # 外层取消（Starlette 收到 FIN 后取消响应任务）：停轮询、关生成器再抛，别留下任务。
        await watcher.stop()
        await _close(audio)
        raise

    async def generate():
        try:
            yield wav_header()
            yield first
            while True:
                try:
                    pcm = await _next_pcm(audio, watcher)
                except StopAsyncIteration:
                    break
                yield pcm
            logger.info("[%s] done in %.1fs", label, time.time() - t0)
        except ClientGone:
            logger.info("[%s] client gone after %.1fs", label, time.time() - t0)
        finally:
            await watcher.stop()
            await _close(audio)

    return StreamingResponse(
        generate(),
        media_type="audio/wav",
        headers={"X-Accel-Buffering": "no"},
    )


def create_app(store: Store, service: ChatService | None = None) -> FastAPI:
    service = service or ChatService(store)
    app = FastAPI(title="NAS LLM hub")
    app.state.store = store
    app.state.service = service
    app.include_router(make_admin_router(store))

    @app.get("/")
    async def health():
        return {"status": "ok"}

    @app.post("/api/v1/stream/text_chat")
    async def text_chat(
        request: Request,
        message: str = Form(...),
        session_id: str = Form("shell"),  # 兼容字段，hub 忽略
        backend: str = Form(""),  # 兼容字段，hub 以数据库当前模型为准
        speed: float = Form(1.0),
        volume: float = Form(1.0),
    ):
        if not message.strip():
            return Response(content=b"", media_type="audio/wav")
        logger.info("[CHAT] question: %s", message)
        audio = service.answer(message.strip(), speed, volume)
        return await stream_wav(request, audio, "text_chat")

    @app.post("/api/v1/tts/stream")
    async def tts_stream(
        request: Request,
        message: str = Form(...),
        speed: float = Form(1.0),
        volume: float = Form(1.0),
    ):
        if not message.strip():
            return Response(content=b"", media_type="audio/wav")
        audio = service.speak(message, speed, volume)
        return await stream_wav(request, audio, "tts_stream")

    @app.get("/api/v1/device/fallback")
    def device_fallback(x_device_token: str = Header("")):
        # 设备直连兜底配置：含 API key，口令不符一律 403；不记录 key 与口令。
        expected = store.get_setting("device_token") or ""
        if not expected or not hmac.compare_digest(
            x_device_token.encode("utf-8"), expected.encode("utf-8")
        ):
            raise HTTPException(status_code=403, detail="forbidden")
        provider = store._current("providers", "fallback_provider_id")
        if provider is None:
            raise HTTPException(status_code=503, detail="no provider configured")
        persona = store.current_persona()
        base_prompt = store.get_setting("base_prompt") or ""
        persona_prompt = persona["prompt"] if persona else ""
        return {
            "base_url": provider["base_url"] or "",
            "api_key": provider["api_key"] or "",
            "model": provider["model"] or "",
            "system_prompt": f"{base_prompt}\n{persona_prompt}",
        }

    return app


def __getattr__(name: str):
    # uvicorn hub.app:app 入口（由 python -m hub 调用）：按需创建，导入模块（测试）不创建数据库
    if name == "app":
        application = create_app(Store())
        globals()["app"] = application
        return application
    raise AttributeError(name)
