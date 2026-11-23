"""SiliconFlow CosyVoice2 客户端测试：用 httpx.MockTransport 断言请求载荷，不访问真实网络。

ffmpeg 转换单独测试（有 ffmpeg 才跑），请求载荷测试通过注入 transport 脱离 ffmpeg。
"""

import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import httpx

from server.tts.siliconflow_tts import (
    COSYVOICE_MODEL,
    SILICONFLOW_BASE_URL,
    SiliconFlowTTSError,
    SiliconFlowTTSClient,
)

VOICE = "speech:sqz:ckvobkgo56:vsmglbrmwwrjdqvogxmw"
KEY = "sk-sf-secret"


def wav_bytes(pcm: bytes, rate: int = 32000) -> bytes:
    import struct
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ",
        16, 1, 1, rate, rate * 2, 2, 16,
        b"data", len(pcm),
    ) + pcm


def handler_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=wav_bytes(b"\x00\x10" * 160))


def handler_error(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text="internal error")


class SiliconFlowTTSClientTest(unittest.IsolatedAsyncioTestCase):
    def make_client(self, handler, **overrides):
        captured = {}

        def recording(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            captured["payload"] = request.content.decode("utf-8")
            captured["headers"] = dict(request.headers)
            return handler(request)

        transport = httpx.MockTransport(recording)
        client = SiliconFlowTTSClient(
            VOICE, KEY, transport=transport,
            base_url=overrides.get("base_url", SILICONFLOW_BASE_URL),
            model=overrides.get("model", COSYVOICE_MODEL),
        )
        return client, captured

    async def test_request_payload_and_headers(self):
        client, captured = self.make_client(handler_ok)
        with unittest.mock.patch.object(client, "_normalize_wav", return_value=wav_bytes(b"\x01\x10" * 8)):
            out = await client.synthesize_pcm("你好", speed=1.25, volume=0.8)
        self.assertEqual(out, wav_bytes(b"\x01\x10" * 8))
        req = captured["request"]
        self.assertEqual(req.url, f"{SILICONFLOW_BASE_URL}/audio/speech")
        self.assertEqual(captured["headers"]["authorization"], f"Bearer {KEY}")
        import json
        payload = json.loads(captured["payload"])
        self.assertEqual(payload["model"], COSYVOICE_MODEL)
        self.assertEqual(payload["input"], "你好")
        self.assertEqual(payload["voice"], VOICE)
        self.assertEqual(payload["response_format"], "wav")
        self.assertEqual(payload["sample_rate"], 32000)
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["speed"], 1.25)
        self.assertEqual(payload["gain"], 0)

    async def test_custom_base_url_and_model(self):
        client, captured = self.make_client(
            handler_ok, base_url="https://custom.example/v1", model="Custom/Model"
        )
        with unittest.mock.patch.object(client, "_normalize_wav", return_value=wav_bytes(b"\x01" * 4)):
            await client.synthesize_pcm("x")
        req = captured["request"]
        self.assertEqual(req.url, "https://custom.example/v1/audio/speech")
        import json
        self.assertEqual(json.loads(captured["payload"])["model"], "Custom/Model")

    async def test_http_error_raises_without_key_leak(self):
        client, captured = self.make_client(handler_error)
        try:
            await client._request_wav("你好", 1.0)
        except SiliconFlowTTSError as exc:
            self.assertIn("500", str(exc))
            self.assertNotIn(KEY, str(exc))
        else:
            self.fail("expected SiliconFlowTTSError")

    async def test_empty_audio_raises(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b""))
        client = SiliconFlowTTSClient(VOICE, KEY, transport=transport)
        with self.assertRaises(SiliconFlowTTSError):
            await client._request_wav("你好", 1.0)

    def test_missing_api_key_raises(self):
        with self.assertRaisesRegex(ValueError, "api_key"):
            SiliconFlowTTSClient(VOICE, "")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg unavailable")
    async def test_real_ffmpeg_normalize_produces_32000_mono_s16_wav(self):
        client, _ = self.make_client(handler_ok)
        # 用真实 ffmpeg 跑一遍归一化：输入 32k WAV，输出应仍是 32k/单声道/16bit。
        pcm = b"\x00\x10" * 1600
        out = client._normalize_wav(wav_bytes(pcm), 32000, 1.0)
        self.assertTrue(out.startswith(b"RIFF"))
        # 解析输出头，确认 32k / 单声道 / 16bit
        import struct
        self.assertEqual(struct.unpack_from("<H", out, 22)[0], 1)  # channels
        self.assertEqual(struct.unpack_from("<I", out, 24)[0], 32000)  # sample rate
        self.assertEqual(struct.unpack_from("<H", out, 34)[0], 16)  # bits


if __name__ == "__main__":
    unittest.main()
