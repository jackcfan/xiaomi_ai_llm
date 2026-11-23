"""真实 ASGI（uvicorn）下的客户端断开回归测试。

单元测试只驱动 ChatService.answer() 并主动 aclose()，测不到 HTTP/ASGI 层的断开：
uvicorn 在客户端离开后 send() 直接丢弃写入（不抛错），只有主动 receive() 才看得到
http.disconnect。这里起一个真 uvicorn，用原始 socket 读完几 KB 就把连接关掉，
断言模型流在 ~2s 内被关闭、且落库为 interrupted=1 的部分回答。

文件末尾的 ``NextPcmDrainTest`` 不经 HTTP，直接测 ``_next_pcm`` 的收尾语义
（断开与外层取消同时发生时既不能丢任务，也不能把取消降级成 ClientGone）。
"""

import asyncio
import os
import socket
import struct
import tempfile
import threading
import time
import types
import unittest

import uvicorn

from hub.app import ClientGone, _next_pcm, create_app
from hub.chat import ChatService
from hub.store import Store
from hub.web import WebResult

# 振幅 0x1000，高于 StreamingPipeline 尾部静音裁剪阈值（96），否则会被裁成空。
PCM = b"\x00\x10" * 1600  # 100ms @32kHz/16bit
SENTENCES = [f"第{i}句话内容。" for i in range(20)]
FULL_ANSWER = "".join(SENTENCES)


def wav(pcm, rate=32000):
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ",
        16, 1, 1, rate, rate * 2, 2, 16,
        b"data", len(pcm),
    ) + pcm


class SlowLLM:
    def __init__(self, owner):
        self.owner = owner

    async def chat_stream(self, message, system_prompt=None, conversation_history=None):
        try:
            for token in self.owner.tokens:
                self.owner.yielded += 1
                yield token
                await asyncio.sleep(self.owner.token_delay)
            self.owner.finished = True
        finally:
            self.owner.closed_at = time.time()


class SlowLLMFactory:
    def __init__(self, tokens=None, token_delay=0.02):
        self.tokens = list(SENTENCES if tokens is None else tokens)
        self.token_delay = token_delay
        self.closed_at = None
        self.finished = False
        self.yielded = 0

    def __call__(self, **kwargs):
        return SlowLLM(self)


class DelayedTTS:
    def __init__(self, owner, voice):
        self.owner = owner

    async def synthesize_pcm(self, text, sample_rate=32000, speed=1.0, volume=1.0):
        self.owner.loop = asyncio.get_running_loop()
        delay = self.owner.first_delay if not self.owner.synthesized else 0.0
        self.owner.synthesized += 1
        if delay:
            await asyncio.sleep(delay)  # 首句 TTS 很慢 == 首块 PCM 要等几秒
        self.owner.texts.append(text)
        return wav(PCM, sample_rate)


class DelayedTTSFactory:
    def __init__(self, first_delay=0.0):
        self.first_delay = first_delay
        self.synthesized = 0
        self.texts = []
        self.loop = None  # 服务端事件循环（synthesize_pcm 在服务端线程里跑时记下）

    def __call__(self, voice):
        return DelayedTTS(self, voice)


class FakeGround:
    async def __call__(self, question, history, provider, on_tool_start=None, **kwargs):
        return WebResult("answer", [], [])


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class HubServer:
    """在后台线程里跑真 uvicorn（不开 lifespan、不写访问日志）。"""

    def __init__(self, app):
        self.app = app
        self.port = free_port()
        self.server = self._make_server(self.port)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def _make_server(self, port):
        return uvicorn.Server(uvicorn.Config(
            self.app, host="127.0.0.1", port=port, log_level="warning",
            lifespan="off", access_log=False,
        ))

    def __enter__(self):
        # free_port() 是先绑定再关闭，端口可能在这之后被抢走：起不来就换端口重试；
        # 就绪判定用 server.started（socket 已绑定），不用试探连接。
        for _ in range(3):
            self.thread.start()
            deadline = time.time() + 10
            while time.time() < deadline:
                if self.server.started:
                    return self
                if not self.thread.is_alive():
                    break  # 绑定失败（端口被占）或启动异常
                time.sleep(0.02)
            self.stop()
            self.port = free_port()
            self.server = self._make_server(self.port)
            self.thread = threading.Thread(target=self.server.run, daemon=True)
        raise AssertionError("uvicorn did not start")

    def stop(self):
        self.server.should_exit = True
        self.server.force_exit = True
        self.thread.join(10)

    def __exit__(self, *exc):
        self.stop()

    def post(self, body: str = "message=hello") -> socket.socket:
        payload = body.encode()
        request = (
            "POST /api/v1/stream/text_chat HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/x-www-form-urlencoded\r\n"
            f"Content-Length: {len(payload)}\r\n\r\n"
        ).encode() + payload
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        sock.sendall(request)
        return sock


class HttpDisconnectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "hub.db"))
        self.llm = SlowLLMFactory()
        self.tts = DelayedTTSFactory()
        self.service = ChatService(
            self.store, llm_factory=self.llm, tts_factory=self.tts, web_ground=FakeGround(),
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # --- helpers ---------------------------------------------------------

    # hub/app.py 里给内部任务起的名字：断开收尾后一个都不该留着。
    HUB_TASKS = {"hub-pcm-next", "hub-pcm-disconnect", "hub-disconnect-poll"}

    def start(self, first_delay=0.0, token_delay=0.02):
        self.tts.first_delay = first_delay
        self.llm.token_delay = token_delay
        return HubServer(create_app(self.store, self.service))

    def wait_for(self, predicate, timeout, what):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return time.time()
            time.sleep(0.02)
        self.fail(f"{what} did not happen within {timeout}s")

    def read_at_least(self, sock, size, timeout=10.0):
        """读到至少 size 字节（或对端关闭/超时）；返回已读内容。"""
        sock.settimeout(timeout)
        received = b""
        deadline = time.time() + timeout
        while len(received) < size and time.time() < deadline:
            chunk = sock.recv(1024)
            if not chunk:
                break
            received += chunk
        self.assertGreaterEqual(len(received), size)
        return received

    def server_task_names(self):
        """服务端事件循环里所有未完成任务的名字（在服务端线程的 loop 上取快照）。"""
        loop = self.tts.loop
        self.assertIsNotNone(loop, "server loop not captured")

        async def names():
            current = asyncio.current_task()
            return {task.get_name() for task in asyncio.all_tasks() if task is not current}

        return asyncio.run_coroutine_threadsafe(names(), loop).result(10)

    def assert_interrupted_turn(self, closed_at):
        turns = self.store.recent_turns(1)
        self.assertEqual(len(turns), 1)
        turn = turns[0]
        self.assertEqual(turn["interrupted"], 1)
        self.assertEqual(turn["kind"], "qa")
        self.assertTrue(turn["answer"])
        self.assertTrue(FULL_ANSWER.startswith(turn["answer"]))
        self.assertNotEqual(turn["answer"], FULL_ANSWER)
        self.assertIsNotNone(closed_at)
        self.assertFalse(self.llm.finished)

    # --- tests -----------------------------------------------------------

    def test_disconnect_before_first_audio_stops_model(self):
        # 首块 PCM 要等 5s（慢 TTS）；客户端 0.3s 就走，模型必须立刻停，而不是等首块。
        with self.start(first_delay=5.0) as server:
            sock = server.post()
            time.sleep(0.3)
            closed_at = time.time()
            sock.close()
            self.wait_for(lambda: self.llm.closed_at is not None, 2.0, "model stream close")
            self.assertLess(self.llm.closed_at - closed_at, 2.0)
            self.wait_for(lambda: self.store.recent_turns(1) != [], 2.0, "interrupted turn save")
            self.assert_interrupted_turn(self.llm.closed_at)

    def test_disconnect_mid_stream_saves_partial(self):
        # 首块很快到达，客户端读完 4KB 断线；模型流与落库都要在 2s 内收尾。
        with self.start(first_delay=0.0) as server:
            sock = server.post()
            self.read_at_least(sock, 4096)
            closed_at = time.time()
            sock.close()
            self.wait_for(lambda: self.llm.closed_at is not None, 2.0, "model stream close")
            self.assertLess(self.llm.closed_at - closed_at, 2.0)
            self.wait_for(lambda: self.store.recent_turns(1) != [], 2.0, "interrupted turn save")
            self.assert_interrupted_turn(self.llm.closed_at)

    def test_disconnect_while_next_chunk_in_flight(self):
        # 模型每句 50ms（比发送慢）：读到 2KB 就断开时 __anext__ 正在飞，Starlette 的
        # listen_for_disconnect 会把取消打在 _next_pcm 的 asyncio.wait 上 —— 这正是
        # round-1 漏掉 __anext__ 任务的交错。断言：模型流关闭、中断落库，且断开后
        # 服务端不遗留任何 hub 内部任务（before/after 对比 asyncio.all_tasks()）。
        with self.start(token_delay=0.05) as server:
            sock = server.post()
            self.read_at_least(sock, 2048)  # 已经出过音频，但回答远未结束
            before = self.server_task_names()
            self.assertTrue(before & self.HUB_TASKS, f"streaming not in progress: {before}")
            closed_at = time.time()
            sock.close()
            self.wait_for(lambda: self.llm.closed_at is not None, 2.0, "model stream close")
            self.assertLess(self.llm.closed_at - closed_at, 2.0)
            self.wait_for(lambda: self.store.recent_turns(1) != [], 2.0, "interrupted turn save")
            self.assert_interrupted_turn(self.llm.closed_at)
            self.wait_for(
                lambda: not (self.server_task_names() & self.HUB_TASKS), 2.0, "hub tasks drained",
            )
            after = self.server_task_names()
            self.assertEqual(after & self.HUB_TASKS, set())
            self.assertEqual(after - before, set(), "tasks left pending after disconnect")


class NextPcmDrainTest(unittest.TestCase):
    """_next_pcm 的收尾语义：断开/外层取消都要把 __anext__ 取消并等它跑完。"""

    HUB_TASKS = {"hub-pcm-next", "hub-pcm-disconnect"}

    def run_case(self, cancel_outer: bool):
        """跑一次"__anext__ 在飞时客户端断开"；返回 (异常, 生成器收尾状态, 遗留任务名)。"""
        async def run():
            gate = asyncio.Event()
            entered = asyncio.Event()
            state = {}

            async def audio():
                try:
                    entered.set()
                    await gate.wait()  # 首块迟迟不出：__anext__ 一直"在飞"
                    yield PCM
                finally:
                    state["closed"] = True
                    await asyncio.sleep(0.05)  # chat._qa 在这里落库并关模型流
                    state["drained"] = True

            watcher = types.SimpleNamespace(gone=asyncio.Event())
            task = asyncio.ensure_future(_next_pcm(audio(), watcher))
            await entered.wait()
            watcher.gone.set()  # 走"客户端断开"分支，进入取消 __anext__ 的收尾
            await asyncio.sleep(0.01)
            if cancel_outer:
                task.cancel()  # 收尾期间外层（Starlette）也取消了
            try:
                await task
            except BaseException as exc:  # noqa: BLE001 - 被断言的对象
                raised = exc
            else:
                raised = None
            leftover = {
                t.get_name()
                for t in asyncio.all_tasks()
                if t is not asyncio.current_task() and t.get_name() in self.HUB_TASKS
            }
            return raised, state, leftover

        return asyncio.run(run())

    def test_client_gone_raises_client_gone_and_drains_next(self):
        raised, state, leftover = self.run_case(cancel_outer=False)
        self.assertIsInstance(raised, ClientGone)
        self.assertTrue(state.get("closed") and state.get("drained"))
        self.assertEqual(leftover, set())

    def test_outer_cancel_during_drain_propagates_cancel(self):
        # 收尾期间的外层取消不能被 suppress(BaseException) 吞掉、降级成 ClientGone。
        raised, state, leftover = self.run_case(cancel_outer=True)
        self.assertIsInstance(raised, asyncio.CancelledError)
        self.assertNotIsInstance(raised, ClientGone)
        self.assertTrue(state.get("closed") and state.get("drained"))
        self.assertEqual(leftover, set())


if __name__ == "__main__":
    unittest.main()
