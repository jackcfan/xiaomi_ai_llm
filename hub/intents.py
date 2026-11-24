"""封闭意图集：key 归代码、文案归后台，执行器按 key 分发。

``intents`` 表（Task 1）里的每一行 key 都必须在此注册一个 handler；后台能改的是
name/instructions/enabled/priority，不能凭空新增 key。新增能力 = 加 handler + 在
capabilities 表播种一行，缺一不可，否则「列出能力」会播报做不到的事。
"""

from __future__ import annotations

from hub.store import Store

QA_LABEL = "qa"

INTENTS: tuple[dict, ...] = (
    {
        "key": "switch_target",
        "name": "切换角色或模型",
        "instructions": "要求换角色、换音色或换回答用的模型",
        "param_kind": "entity",
    },
    {
        "key": "reset_context",
        "name": "重置对话",
        "instructions": "要求忘掉之前的对话、重新开始、换个话题",
        "param_kind": "none",
    },
    {
        "key": "list_capabilities",
        "name": "列出能力",
        "instructions": "询问助手自己会什么、有哪些技能",
        "param_kind": "none",
    },
)

INTENT_KEYS: tuple[str, ...] = tuple(i["key"] for i in INTENTS)


def _switch_target(store: Store, params: dict) -> tuple[str, str]:
    kind = params.get("kind")
    name = params.get("name") or ""
    if kind == "persona":
        store.set_setting("current_persona_id", str(params["id"]))
        return f"好的，已切换为{name}", "command"
    if kind == "provider":
        store.set_setting("current_provider_id", str(params["id"]))
        return f"好的，已切换到{name}", "command"
    return "抱歉，我不太确定要切换什么。", "command"


def _reset_context(store: Store, params: dict) -> tuple[str, str]:
    return "好的，我们重新开始", "reset"


def _list_capabilities(store: Store, params: dict) -> tuple[str, str]:
    names = [c["name"] for c in store.list_capabilities() if c.get("enabled")]
    joined = "、".join(names)
    return f"我可以帮你{joined}，还能回答问题和联网查资料。", "command"


HANDLERS = {
    "switch_target": _switch_target,
    "reset_context": _reset_context,
    "list_capabilities": _list_capabilities,
}


def sync_intents(store: Store) -> None:
    """按代码常量 upsert 意图：新增插入，已有行同步 name/param_kind，仅在
    instructions 为空时补默认值，enabled/priority 留给后台。"""
    for intent in INTENTS:
        row = store.get_intent(intent["key"])
        if row is None:
            store.upsert_intent(intent)
            continue
        merged = {
            "key": intent["key"],
            "name": intent["name"],
            "param_kind": intent["param_kind"],
            "instructions": row.get("instructions") or intent["instructions"],
            "enabled": row.get("enabled", 1),
            "priority": row.get("priority", 0),
        }
        store.upsert_intent(merged)


def execute(store: Store, intent_key: str, params: dict) -> tuple[str, str]:
    """执行一个意图，返回 (播报文本, turns.kind)。未知 key 抛 KeyError——调用方应
    保证 intent_key 来自识别链（只可能是 INTENT_KEYS 或 qa）。"""
    return HANDLERS[intent_key](store, params)
