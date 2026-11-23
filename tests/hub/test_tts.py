import asyncio
import time
import unittest

from hub.chat import ChatService
from hub.tts import ThreadedTTS, edge_tts_factory


class BlockingTTS:
    """模拟 EdgeTTSClient：协程里同步阻塞（如 subprocess.run 调 ffmpeg）。"""

    voice = "zh-CN-YunxiNeural"

    def __init__(self):
        self.calls = []

    async def synthesize_pcm(self, text, sample_rate=32000, speed=1.0, volume=1.0):
        self.calls.append((text, sample_rate, speed, volume))
        time.sleep(0.3)
        return b"RIFF-" + text.encode("utf-8")


class ThreadedTTSTest(unittest.IsolatedAsyncioTestCase):
    async def test_event_loop_stays_responsive_during_blocking_synthesis(self):
        inner = BlockingTTS()
        tts = ThreadedTTS(inner)
        ticks = 0
        stop = asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        try:
            result = await tts.synthesize_pcm("你好", sample_rate=16000, speed=1.2, volume=0.8)
        finally:
            stop.set()
            await task
        self.assertEqual(result, "RIFF-你好".encode("utf-8"))
        self.assertEqual(inner.calls, [("你好", 16000, 1.2, 0.8)])
        # 0.3 s 阻塞期间 ticker 每 10 ms 跑一次；直接 await 原实现时 ticks 只有 1。
        self.assertGreaterEqual(ticks, 10)

    async def test_unwrapped_blocking_synthesis_freezes_loop(self):
        """对照组：证明上面的断言确实能区分阻塞与不阻塞。"""
        ticks = 0
        stop = asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        # 测试循环处于 debug 模式，会把这次 0.3 s 的卡顿记成 asyncio 慢回调警告。
        with self.assertLogs("asyncio", "WARNING"):
            try:
                await BlockingTTS().synthesize_pcm("你好")
            finally:
                stop.set()
                await task
        self.assertLess(ticks, 10)

    def test_attributes_pass_through(self):
        self.assertEqual(ThreadedTTS(BlockingTTS()).voice, "zh-CN-YunxiNeural")

    def test_chat_service_defaults_to_threaded_edge_tts(self):
        self.assertIs(ChatService(store=None).tts_factory, edge_tts_factory)


if __name__ == "__main__":
    unittest.main()
