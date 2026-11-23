"""SQLite-backed configuration and history store for the NAS LLM hub."""

import json
import logging
import os
import secrets
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

BASE_PROMPT = "你是一个语音助手，请用纯文本口语直接回答，不要复述问题，不要使用任何 Markdown 格式或列表，说完整的句子。"

SEED_PERSONAS = [
    ("默认", "说话自然、简洁、友好，像一个靠谱的朋友。", "zh-CN-YunjianNeural"),
    ("温柔", "说话温柔体贴、语气轻柔，多给人安慰和鼓励。", "zh-CN-XiaoxiaoNeural"),
    ("毒舌", "说话犀利幽默、爱吐槽，但不恶意伤人，最后仍然给出有用的回答。", "zh-CN-YunxiNeural"),
    ("撒娇", "说话软萌可爱、爱撒娇，语气活泼，偶尔带点语气词。", "zh-CN-XiaoyiNeural"),
]

PROVIDER_FIELDS = (
    "name", "aliases", "base_url", "api_key", "model", "temperature", "max_tokens", "extra_body",
)
# 默认 DeepSeek 关闭隐藏思考：与设备直连一致（thinking disabled，首句约 2s），避免开口变慢。
DEEPSEEK_EXTRA_BODY = {"thinking": {"type": "disabled"}}
PERSONA_FIELDS = ("name", "aliases", "prompt", "voice")
TURN_KINDS = ("qa", "command", "reset")

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    temperature REAL,
    max_tokens INTEGER,
    extra_body TEXT
);
CREATE TABLE IF NOT EXISTS personas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    voice TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    user_text TEXT NOT NULL DEFAULT '',
    answer TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    interrupted INTEGER NOT NULL DEFAULT 0,
    provider_name TEXT NOT NULL DEFAULT '',
    persona_name TEXT NOT NULL DEFAULT '',
    web_sources TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS turns_ts ON turns (ts);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            self._migrate()
            if self._conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0:
                self._seed()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        """给旧库补新列（CREATE TABLE IF NOT EXISTS 不会改已有表）。旧行的新列为 NULL。"""
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(providers)")}
        if "extra_body" not in columns:
            self._conn.execute("ALTER TABLE providers ADD COLUMN extra_body TEXT")
            log.info("providers: added column extra_body")

    def _seed(self) -> None:
        cur = self._conn.execute(
            "INSERT INTO providers (name, aliases, base_url, api_key, model, extra_body) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "DeepSeek",
                "deepseek,深度求索",
                "https://api.deepseek.com",
                os.environ.get("DEEPSEEK_API_KEY", ""),
                "deepseek-flash",
                json.dumps(DEEPSEEK_EXTRA_BODY),
            ),
        )
        provider_id = str(cur.lastrowid)
        persona_ids = []
        for name, prompt, voice in SEED_PERSONAS:
            cur = self._conn.execute(
                "INSERT INTO personas (name, aliases, prompt, voice) VALUES (?, ?, ?, ?)",
                (name, "", prompt, voice),
            )
            persona_ids.append(cur.lastrowid)
        settings = {
            "current_provider_id": provider_id,
            "current_persona_id": str(persona_ids[0]),
            "fallback_provider_id": provider_id,
            "search_provider_id": provider_id,
            "context_gap_seconds": "300",
            "context_max_turns": "10",
            "base_prompt": BASE_PROMPT,
            "device_token": secrets.token_urlsafe(16),
        }
        self._conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?)", settings.items()
        )

    # --- generic helpers -------------------------------------------------

    def _list(self, table: str) -> list[dict]:
        rows = self._conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def _get(self, table: str, id: int) -> dict | None:
        row = self._conn.execute(f"SELECT * FROM {table} WHERE id = ?", (id,)).fetchone()
        return dict(row) if row else None

    def _upsert(self, table: str, fields: tuple, data: dict) -> int:
        values = {f: data[f] for f in fields if f in data}
        with self._lock, self._conn:
            row_id = data.get("id")
            if row_id is not None and self._get(table, int(row_id)) is not None:
                if values:
                    assigns = ", ".join(f"{f} = ?" for f in values)
                    self._conn.execute(
                        f"UPDATE {table} SET {assigns} WHERE id = ?",
                        (*values.values(), int(row_id)),
                    )
                return int(row_id)
            cols = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            cur = self._conn.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values())
            )
            return cur.lastrowid

    def _delete(self, table: str, id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(f"DELETE FROM {table} WHERE id = ?", (id,))

    def _current(self, table: str, setting: str) -> dict | None:
        """Row the setting points to; falls back to the lowest-id row if missing."""
        value = self.get_setting(setting)
        if value is not None and value.isdecimal():
            row = self._get(table, int(value))
            if row is not None:
                return row
        row = self._conn.execute(f"SELECT * FROM {table} ORDER BY id LIMIT 1").fetchone()
        fallback = dict(row) if row else None
        log.warning(
            "%s: %s=%r not found, falling back to id %s",
            table,
            setting,
            value,
            fallback["id"] if fallback else None,
        )
        return fallback

    # --- providers -------------------------------------------------------

    def list_providers(self) -> list[dict]:
        return self._list("providers")

    def get_provider(self, id: int) -> dict | None:
        return self._get("providers", id)

    def upsert_provider(self, data: dict) -> int:
        return self._upsert("providers", PROVIDER_FIELDS, data)

    def delete_provider(self, id: int) -> None:
        self._delete("providers", id)

    def current_provider(self) -> dict | None:
        return self._current("providers", "current_provider_id")

    # --- personas --------------------------------------------------------

    def list_personas(self) -> list[dict]:
        return self._list("personas")

    def get_persona(self, id: int) -> dict | None:
        return self._get("personas", id)

    def upsert_persona(self, data: dict) -> int:
        return self._upsert("personas", PERSONA_FIELDS, data)

    def delete_persona(self, id: int) -> None:
        self._delete("personas", id)

    def current_persona(self) -> dict | None:
        return self._current("personas", "current_persona_id")

    # --- settings --------------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # --- history ---------------------------------------------------------

    def add_turn(
        self,
        user_text: str,
        answer: str,
        provider_name: str,
        persona_name: str,
        kind: str,
        web_sources: str = "",
        interrupted: bool = False,
        ts: float | None = None,
    ) -> int:
        if kind not in TURN_KINDS:
            raise ValueError(f"invalid turn kind: {kind!r}")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO turns (ts, user_text, answer, kind, interrupted, "
                "provider_name, persona_name, web_sources) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time() if ts is None else ts,
                    user_text,
                    answer,
                    kind,
                    1 if interrupted else 0,
                    provider_name,
                    persona_name,
                    web_sources,
                ),
            )
            return cur.lastrowid

    def recent_turns(self, limit: int) -> list[dict]:
        """Newest first."""
        return self.list_turns(0, limit)

    def list_turns(self, offset: int, limit: int) -> list[dict]:
        """Newest first, paginated."""
        rows = self._conn.execute(
            "SELECT * FROM turns ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
