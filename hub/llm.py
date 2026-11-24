"""OpenAI 兼容的流式聊天客户端（DeepSeek / Kimi / 其他兼容服务）。

接口与 server/streaming_pipeline.StreamingPipeline.process 调用的
``llm.chat_stream(message=..., system_prompt=..., conversation_history=...)`` 一致。
关闭生成器（aclose）会立即关闭底层 HTTP 流，用于音箱断开时停止模型生成。
API Key 只放在请求头里，不写日志，也不进入异常消息。
"""

from __future__ import annotations

import json
import logging
import time
from typing import AsyncIterator

import httpx

logger = logging.getLogger(__name__)

_ERROR_BODY_LIMIT = 200


class LLMError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"LLM HTTP {status}: {body}")


class ChatClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 45.0,
        extra_body: dict | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._transport = transport
        self.timeout = timeout
        # 按供应商附加到请求体的字段（如 DeepSeek 的 {"thinking": {"type": "disabled"}}，
        # 关闭隐藏思考以免拖慢首句）；不能覆盖 model / messages / stream。
        self.extra_body = dict(extra_body or {})

    def __repr__(self) -> str:  # 不暴露 api_key
        return f"ChatClient(base_url={self.base_url!r}, model={self.model!r})"

    def _build_messages(
        self, message: str, system_prompt: str | None, conversation_history: list | None
    ) -> list:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": message})
        return messages

    async def chat_stream(
        self,
        message: str,
        system_prompt: str | None = None,
        conversation_history: list | None = None,
    ) -> AsyncIterator[str]:
        url = f"{self.base_url}/chat/completions"
        payload: dict = dict(self.extra_body)
        payload.update({
            "model": self.model,
            "messages": self._build_messages(message, system_prompt, conversation_history),
            "stream": True,
        })
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "text/event-stream",
        }

        started = time.monotonic()
        chars = 0
        logger.info("call model=%s base_url=%s", self.model, self.base_url)
        async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if not resp.is_success:
                    raw = await resp.aread()
                    body = raw.decode("utf-8", errors="replace")
                    if self._api_key:
                        body = body.replace(self._api_key, "***")
                    body = body[:_ERROR_BODY_LIMIT]
                    logger.warning(
                        "LLM 失败 model=%s status=%s elapsed=%.0fms: %s",
                        self.model, resp.status_code, (time.monotonic() - started) * 1000, body,
                    )
                    raise LLMError(resp.status_code, body)

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        logger.info(
                            "done model=%s status=%s chars=%d elapsed=%.0fms",
                            self.model, resp.status_code, chars, (time.monotonic() - started) * 1000,
                        )
                        return
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        logger.debug("跳过无法解析的 SSE 行")
                        continue
                    choices = chunk.get("choices") if isinstance(chunk, dict) else None
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if content:
                        chars += len(content)
                        yield content
                logger.info(
                    "done model=%s status=%s chars=%d elapsed=%.0fms",
                    self.model, resp.status_code, chars, (time.monotonic() - started) * 1000,
                )
