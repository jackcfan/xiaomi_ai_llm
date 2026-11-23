"""hub 侧 TTS 包装：把整次合成放到工作线程里跑，避免阻塞事件循环。

server/tts/edge_tts_client.EdgeTTSClient.synthesize 在协程里同步调用 ffmpeg
（``subprocess.run``），每句都会卡住事件循环，回答期间 ``/`` 健康检查等请求都无响应。
这里不改 server/，而是让 StreamingPipeline 调用的 ``synthesize_pcm`` 在线程中
以独立事件循环（``asyncio.run``）执行原实现。CosyVoice2（硅基流动）同样在
``SiliconFlowTTSClient`` 里同步调 ffmpeg，因此也走同一个 ThreadedTTS 包装。
"""

from __future__ import annotations

import asyncio

from server.tts.edge_tts_client import EdgeTTSClient
from server.tts.siliconflow_tts import SiliconFlowTTSClient


class ThreadedTTS:
    """包装任意带 ``async synthesize_pcm`` 的 TTS 客户端，在工作线程中执行。"""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):  # voice 等属性透传
        return getattr(self.inner, name)

    async def synthesize_pcm(
        self, text: str, sample_rate: int = 32000, speed: float = 1.0, volume: float = 1.0,
    ) -> bytes:
        def run() -> bytes:
            # 协程在线程内创建并执行，调用方被取消时也不会留下未 await 的协程。
            return asyncio.run(self.inner.synthesize_pcm(
                text, sample_rate=sample_rate, speed=speed, volume=volume,
            ))

        return await asyncio.to_thread(run)


def edge_tts_factory(voice: str) -> ThreadedTTS:
    """EdgeTTSClient + 线程执行。"""
    return ThreadedTTS(EdgeTTSClient(voice))


def siliconflow_tts_factory(voice: str, api_key: str, base_url: str = "") -> ThreadedTTS:
    """SiliconFlowTTSClient（CosyVoice2）+ 线程执行。模型固定为 CosyVoice2。"""
    return ThreadedTTS(SiliconFlowTTSClient(voice, api_key, base_url=base_url))
