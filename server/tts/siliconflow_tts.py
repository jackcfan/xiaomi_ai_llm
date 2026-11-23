"""硅基流动（SiliconFlow）CosyVoice2 语音合成客户端。

调用 ``POST https://api.siliconflow.cn/v1/audio/speech``，``response_format=wav`` +
``sample_rate=32000``，响应体为二进制 WAV（比 ``pcm`` 更稳，自带位深/声道/采样率，也比
``mp3`` 少一次解码）。拿到后再走与 EdgeTTS 相同的 ffmpeg 响度归一化滤镜，输出
32 kHz / 16-bit / 单声道 WAV（PCM），供 ``StreamingPipeline`` 直接抽取 PCM。

``synthesize_pcm`` 在协程里同步调用 ``subprocess.run``（ffmpeg），与 ``EdgeTTSClient``
一致——由 ``hub.tts.ThreadedTTS`` 放进工作线程执行，避免阻塞事件循环。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile

import httpx

log = logging.getLogger(__name__)

SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
COSYVOICE_MODEL = "FunAudioLLM/CosyVoice2-0.5B"
ENGINE = "siliconflow"

# 与 EdgeTTS 相同的响度归一化滤镜：dynaudnorm 抬平均响度、loudnorm 压到 -14 LUFS、
# limiter 防削波，让不同引擎的音色在音箱上响度一致。改这里时记得同步 edge_tts_client。
_NORMALIZE_FILTERS = [
    "dynaudnorm=f=150:g=15:p=0.95:m=10",
    "loudnorm=I=-14:TP=-1.0:LRA=7",
    "alimiter=limit=0.95",
]


class SiliconFlowTTSError(RuntimeError):
    """CosyVoice2 合成失败（HTTP 非 2xx 或返回空音频）。"""


class SiliconFlowTTSClient:
    def __init__(
        self,
        voice: str,
        api_key: str,
        base_url: str = SILICONFLOW_BASE_URL,
        model: str = COSYVOICE_MODEL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ):
        if not api_key:
            raise ValueError("SiliconFlow TTS 需要 api_key")
        self.voice = voice
        self.api_key = api_key
        self.base_url = (base_url or SILICONFLOW_BASE_URL).rstrip("/")
        self.model = model or COSYVOICE_MODEL
        self._transport = transport
        self.timeout = timeout
        # ffmpeg 懒加载：只在真正转换音频时才要求存在，便于请求载荷的单测脱离 ffmpeg。
        self._ffmpeg = shutil.which("ffmpeg")

    async def _request_wav(self, text: str, speed: float) -> bytes:
        payload = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": "wav",
            "sample_rate": 32000,
            "stream": False,
            "speed": speed,
            "gain": 0,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}/audio/speech", json=payload, headers=headers)
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", errors="replace")
                if self.api_key:
                    body = body.replace(self.api_key, "***")
                log.warning("SiliconFlow TTS 返回 HTTP %s", resp.status_code)
                raise SiliconFlowTTSError(
                    f"SiliconFlow TTS HTTP {resp.status_code}: {body[:200]}"
                )
            data = resp.content
        if not data:
            raise SiliconFlowTTSError("SiliconFlow TTS returned empty audio")
        return data

    def _normalize_wav(self, wav_data: bytes, sample_rate: int, volume: float) -> bytes:
        ffmpeg = self._ffmpeg or shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError(
                "SiliconFlow TTS requires ffmpeg on PATH; install ffmpeg and verify "
                "`ffmpeg -version` before starting the hub"
            )
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as inf:
            inf.write(wav_data)
            in_path = inf.name
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as outf:
            out_path = outf.name
        try:
            filters = list(_NORMALIZE_FILTERS)
            if volume != 1.0:
                filters.append(f"volume={volume}")
            cmd = [ffmpeg, "-y", "-i", in_path, "-filter:a", ",".join(filters)]
            cmd.extend(["-f", "wav", "-acodec", "pcm_s16le", "-ar", str(sample_rate), "-ac", "1", out_path])
            subprocess.run(cmd, capture_output=True, timeout=30, check=True)
            with open(out_path, "rb") as f:
                return f.read()
        finally:
            os.unlink(in_path)
            os.unlink(out_path)

    async def synthesize_pcm(
        self, text: str, sample_rate: int = 32000, speed: float = 1.0, volume: float = 1.0
    ) -> bytes:
        """合成 WAV（32 kHz/16-bit/单声道，含 RIFF 头），失败抛 SiliconFlowTTSError。"""
        wav_data = await self._request_wav(text, speed)
        return self._normalize_wav(wav_data, sample_rate, volume)
