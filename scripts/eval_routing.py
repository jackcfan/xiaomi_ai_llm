"""决策路由评测脚本（手动执行，允许联网，不在 tests/ 下）。

读 tests/hub/fixtures/routing_cases.json，对每条句子走真实的三级路由
（规则 → 决策模型 → 问答），打印 text | expected | actual | confidence | 耗时，
最后输出准确率。用法：

    PYTHON=.venv/Scripts/python.exe .venv/Scripts/python.exe scripts/eval_routing.py

前置：MySQL 里 decision_provider_id 已指向百炼决策模型行，且 FN_MYSQL_PWD 已设置。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from hub.decision import DecisionClient
from hub.store import Store

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "hub" / "fixtures" / "routing_cases.json"


async def main() -> int:
    store = Store()
    try:
        provider_id = store.get_setting("decision_provider_id")
        provider = store.get_provider(int(provider_id)) if provider_id and provider_id.isdecimal() else None
        if provider is None:
            print("decision_provider_id 未配置或指向不存在的 provider", file=sys.stderr)
            return 1
        client = DecisionClient(
            base_url=provider["base_url"], api_key=provider["api_key"],
            model=provider.get("model") or "decision-model-preview",
        )
        cases = json.loads(FIXTURE.read_text("utf-8"))
        correct = 0
        total = 0
        for case in cases:
            text = case["text"]
            expected = f"{case['route']}/{case['intent_key']}"
            started = time.monotonic()
            from hub.chat import ChatService
            service = ChatService(store, web_ground=None)
            route = await service._route(text)
            elapsed = (time.monotonic() - started) * 1000
            actual = f"{route.route}/{route.intent_key or 'qa'}"
            ok = actual == expected
            if ok:
                correct += 1
            total += 1
            conf = route.decision.get("confidence", "") if route.decision else ""
            print(f"{'OK ' if ok else 'XX '} | {text!r:40} | {expected:20} | {actual:20} | "
                  f"conf={conf} | {elapsed:.0f}ms")
        print(f"\n准确率：{correct}/{total}")
        return 0 if correct == total else 2
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
