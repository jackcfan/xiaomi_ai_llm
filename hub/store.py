"""hub 的持久化存储：生产用 MySQL，测试用 SQLite。

生产（``Store()``，``dsn`` 为空）连接 NAS MySQL。连接参数从环境变量读取，密码只从
``FN_MYSQL_PWD`` 取（绝不写进代码或仓库）：

- ``HUB_DB_HOST``（默认 ``fnos.itougu.tech``）/ ``HUB_DB_PORT``（默认 3306）
- ``HUB_DB_NAME``（默认 ``xiaomi_hub``）/ ``HUB_DB_USER``（默认 ``xiaomi_hub_user``）

启动时自动建表（``CREATE TABLE IF NOT EXISTS``）；``settings`` 表为空时播种默认
服务商（DeepSeek）与默认人设。

测试（``Store(path)``，``dsn`` 非空）仍用 SQLite 文件，避免访问真实网络
（见 CLAUDE.md「tests/hub 不得访问真实网络」）。两类后端共用同一套公开方法，
SQL 差异收敛在占位符（SQLite ``?`` / MySQL ``%s``）与建表语句上。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:  # 仅 SQLite 测试路径允许缺少 PyMySQL；生产连 MySQL 时会给明确报错
    pymysql = None  # type: ignore[assignment]
    DictCursor = None  # type: ignore[assignment]

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
PERSONA_FIELDS = ("name", "aliases", "prompt", "voice", "tts_provider")
# TTS 服务提供方：空串或 "edgetts" 用 EdgeTTS（免费、无需 key），"siliconflow" 用
# CosyVoice2（key 存 providers 表、按名/别名匹配）。personas.voice 始终是声音源参数：
# EdgeTTS 是音色名（如 zh-CN-YunjianNeural），CosyVoice2 是 voice id（如 speech:sqz:...）。
TTS_PROVIDERS = ("edgetts", "siliconflow")
TTS_PROVIDER_DEFAULT = ""  # 存库用空串表示 EdgeTTS（旧行迁移后即为空，语义等同 "edgetts"）
TURN_KINDS = ("qa", "command", "reset")

DEFAULT_HOST = "fnos.itougu.tech"
DEFAULT_PORT = 3306
DEFAULT_DB = "xiaomi_hub"
DEFAULT_USER = "xiaomi_hub_user"


class ConfigError(RuntimeError):
    """MySQL 连接配置缺失/非法（例如 FN_MYSQL_PWD 未设置）。"""


def mysql_params(env: dict | None = None) -> dict:
    """从环境变量拼出 pymysql.connect 的连接参数；密码只从 FN_MYSQL_PWD 取。"""
    env = os.environ if env is None else env
    password = env.get("FN_MYSQL_PWD")
    if not password:
        raise ConfigError(
            "FN_MYSQL_PWD 未设置：hub 需要它作为 xiaomi_hub_user 的 MySQL 密码"
            "（只从环境变量读取，不写入工程）"
        )
    return {
        "host": env.get("HUB_DB_HOST", DEFAULT_HOST),
        "port": int(env.get("HUB_DB_PORT", str(DEFAULT_PORT))),
        "user": env.get("HUB_DB_USER", DEFAULT_USER),
        "password": password,
        "database": env.get("HUB_DB_NAME", DEFAULT_DB),
        "charset": "utf8mb4",
    }


# ``key`` 是 MySQL 保留字，MySQL 侧一律用反引号；SQLite 同样接受反引号，故通用 SQL
# 里直接写反引号即可两用。settings.value 只放字符串。
SCHEMA_SQLITE = """
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
    voice TEXT NOT NULL DEFAULT '',
    tts_provider TEXT NOT NULL DEFAULT ''
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

# MySQL 8 的 TEXT 列不能带 DEFAULT，故不写默认值；写入方（add_turn / 播种 / 后台表单）
# 总会显式提供这些字段。
SCHEMA_MYSQL = (
    """
    CREATE TABLE IF NOT EXISTS providers (
        id BIGINT NOT NULL AUTO_INCREMENT,
        name VARCHAR(255) NOT NULL,
        aliases VARCHAR(1024) NOT NULL DEFAULT '',
        base_url VARCHAR(2048) NOT NULL DEFAULT '',
        api_key VARCHAR(2048) NOT NULL DEFAULT '',
        model VARCHAR(255) NOT NULL DEFAULT '',
        temperature DOUBLE NULL,
        max_tokens INT NULL,
        extra_body TEXT NULL,
        PRIMARY KEY (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS personas (
        id BIGINT NOT NULL AUTO_INCREMENT,
        name VARCHAR(255) NOT NULL,
        aliases VARCHAR(1024) NOT NULL DEFAULT '',
        prompt TEXT NOT NULL,
        voice VARCHAR(255) NOT NULL DEFAULT '',
        tts_provider VARCHAR(32) NOT NULL DEFAULT '',
        PRIMARY KEY (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        `key` VARCHAR(255) NOT NULL,
        `value` TEXT NOT NULL,
        PRIMARY KEY (`key`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS turns (
        id BIGINT NOT NULL AUTO_INCREMENT,
        ts DOUBLE NOT NULL,
        user_text TEXT NOT NULL,
        answer TEXT NOT NULL,
        kind VARCHAR(16) NOT NULL,
        interrupted TINYINT NOT NULL DEFAULT 0,
        provider_name VARCHAR(255) NOT NULL DEFAULT '',
        persona_name VARCHAR(255) NOT NULL DEFAULT '',
        web_sources TEXT NOT NULL,
        PRIMARY KEY (id),
        KEY idx_turns_ts (ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
)


class Store:
    """hub 的配置/历史存储。``dsn`` 非空 → SQLite 文件；为空 → 环境变量配好的 MySQL。"""

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn
        self._dialect, self._params = self._resolve(dsn)
        # 单一连接跨线程（uvicorn 同步后台走线程池、异步接口走事件循环）共享：
        # 所有数据库访问都串行化在 RLock 里，避免 pymysql 连接并发使用。
        self._lock = threading.RLock()
        self._conn = self._connect()
        self._init_schema()

    # --- 连接与建表 ------------------------------------------------------

    @staticmethod
    def _resolve(dsn: str | None) -> tuple[str, dict]:
        if dsn:
            return "sqlite", {"path": dsn}
        return "mysql", mysql_params()

    def _connect(self):
        if self._dialect == "sqlite":
            conn = sqlite3.connect(self._params["path"], check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn
        if pymysql is None:
            raise ConfigError("缺少 PyMySQL：hub 现在用 MySQL，请先安装 pymysql（见 requirements-tts.txt）")
        try:
            return pymysql.connect(**self._params, cursorclass=DictCursor, autocommit=True)
        except pymysql.err.OperationalError as exc:
            if getattr(exc, "args", None) and exc.args[0] == 1045:
                raise ConfigError(
                    "MySQL 登录被拒绝（1045）。请检查 FN_MYSQL_PWD 是否正确、"
                    "且 xiaomi_hub_user 被允许从当前来源 IP 连接（服务端未做 IP 白名单）。"
                ) from exc
            raise

    def _reconnect(self):
        return pymysql.connect(**self._params, cursorclass=DictCursor, autocommit=True)

    def _conn_for(self):
        if self._dialect != "mysql":
            return self._conn
        conn = self._conn
        if conn is None:
            return self._reconnect()
        try:
            conn.ping(reconnect=True)
        except Exception:
            return self._reconnect()
        return conn

    def _sql(self, sql: str) -> str:
        return sql if self._dialect == "sqlite" else sql.replace("?", "%s")

    def _fetchone(self, sql: str, params=()) -> dict | None:
        with self._lock:
            cur = self._conn_for().cursor()
            cur.execute(self._sql(sql), params)
            row = cur.fetchone()
            cur.close()
            return dict(row) if row else None

    def _fetchall(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            cur = self._conn_for().cursor()
            cur.execute(self._sql(sql), params)
            rows = cur.fetchall()
            cur.close()
            return [dict(r) for r in rows]

    def _execute(self, sql: str, params=()) -> int:
        """执行写语句，返回 lastrowid（INSERT 时为自增主键）。SQLite 显式提交。"""
        with self._lock:
            conn = self._conn_for()
            cur = conn.cursor()
            cur.execute(self._sql(sql), params)
            if self._dialect == "sqlite":
                conn.commit()
            lastrowid = cur.lastrowid
            cur.close()
            return lastrowid

    def _executemany(self, sql: str, seq) -> None:
        with self._lock:
            conn = self._conn_for()
            cur = conn.cursor()
            cur.executemany(self._sql(sql), seq)
            if self._dialect == "sqlite":
                conn.commit()
            cur.close()

    def _init_schema(self) -> None:
        with self._lock:
            if self._dialect == "sqlite":
                self._conn.executescript(SCHEMA_SQLITE)
                self._migrate_sqlite()
            else:
                for stmt in SCHEMA_MYSQL:
                    self._execute(stmt)
                self._migrate_mysql()
            if self._count_settings() == 0:
                self._seed()

    def _migrate_sqlite(self) -> None:
        """给旧库补新列（CREATE TABLE IF NOT EXISTS 不会改已有表）。旧行的新列为 NULL。"""
        cur = self._conn.cursor()
        cur.execute("PRAGMA table_info(providers)")
        columns = {row["name"] for row in cur.fetchall()}
        cur.close()
        if "extra_body" not in columns:
            self._execute("ALTER TABLE providers ADD COLUMN extra_body TEXT")
            log.info("providers: added column extra_body")
        cur = self._conn.cursor()
        cur.execute("PRAGMA table_info(personas)")
        pcolumns = {row["name"] for row in cur.fetchall()}
        cur.close()
        if "tts_provider" not in pcolumns:
            self._execute("ALTER TABLE personas ADD COLUMN tts_provider TEXT NOT NULL DEFAULT ''")
            log.info("personas: added column tts_provider")

    def _migrate_mysql(self) -> None:
        for table, column in (("providers", "extra_body"), ("personas", "tts_provider")):
            row = self._fetchone(
                "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
                f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{table}' AND COLUMN_NAME = '{column}'"
            )
            if not row or row["n"] == 0:
                if column == "extra_body":
                    self._execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NULL")
                else:
                    self._execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR(32) NOT NULL DEFAULT ''"
                    )
                log.info("%s: added column %s", table, column)

    def _count_settings(self) -> int:
        row = self._fetchone("SELECT COUNT(*) AS n FROM settings")
        return row["n"] if row else 0

    def _seed(self) -> None:
        provider_id = self._execute(
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
        persona_ids = []
        for name, prompt, voice in SEED_PERSONAS:
            persona_ids.append(self._execute(
                "INSERT INTO personas (name, aliases, prompt, voice) VALUES (?, ?, ?, ?)",
                (name, "", prompt, voice),
            ))
        settings = {
            "current_provider_id": str(provider_id),
            "current_persona_id": str(persona_ids[0]),
            "fallback_provider_id": str(provider_id),
            "search_provider_id": str(provider_id),
            "context_gap_seconds": "300",
            "context_max_turns": "10",
            "base_prompt": BASE_PROMPT,
            "device_token": secrets.token_urlsafe(16),
        }
        self._executemany(
            "INSERT INTO settings (`key`, `value`) VALUES (?, ?)", list(settings.items())
        )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # --- generic helpers -------------------------------------------------

    def _list(self, table: str) -> list[dict]:
        return self._fetchall(f"SELECT * FROM {table} ORDER BY id")

    def _get(self, table: str, id: int) -> dict | None:
        return self._fetchone(f"SELECT * FROM {table} WHERE id = ?", (id,))

    def _upsert(self, table: str, fields: tuple, data: dict) -> int:
        values = {f: data[f] for f in fields if f in data}
        with self._lock:
            row_id = data.get("id")
            if row_id is not None and self._get(table, int(row_id)) is not None:
                if values:
                    assigns = ", ".join(f"{f} = ?" for f in values)
                    self._execute(
                        f"UPDATE {table} SET {assigns} WHERE id = ?",
                        (*values.values(), int(row_id)),
                    )
                return int(row_id)
            cols = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            return self._execute(
                f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values())
            )

    def _delete(self, table: str, id: int) -> None:
        with self._lock:
            self._execute(f"DELETE FROM {table} WHERE id = ?", (id,))

    def _current(self, table: str, setting: str) -> dict | None:
        """Row the setting points to; falls back to the lowest-id row if missing."""
        value = self.get_setting(setting)
        if value is not None and value.isdecimal():
            row = self._get(table, int(value))
            if row is not None:
                return row
        row = self._fetchone(f"SELECT * FROM {table} ORDER BY id LIMIT 1")
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

    def persona_tts_engine(self, persona: dict) -> str:
        """persona 的 TTS 引擎关键字（``edgetts`` / ``siliconflow``）；空串/未知回退 edgetts。"""
        value = str(persona.get("tts_provider") or "").strip().lower()
        return value if value in TTS_PROVIDERS else "edgetts"

    def find_tts_provider(self, engine: str) -> dict | None:
        """按名/别名匹配 TTS 服务提供方对应的 provider 行（CosyVoice2 的 key 存 providers 表）。"""
        key = (engine or "").strip().lower()
        for provider in self.list_providers():
            names = [provider.get("name") or ""]
            names += (provider.get("aliases") or "").replace("，", ",").split(",")
            if key in {n.strip().lower() for n in names if n.strip()}:
                return provider
        return None

    # --- settings --------------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._fetchone("SELECT `value` FROM settings WHERE `key` = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            if self._dialect == "sqlite":
                self._execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
            else:
                # MySQL 8.0.19+ 的行别名写法（不用已弃用的 VALUES() 函数）。
                self._execute(
                    "INSERT INTO settings (`key`, `value`) VALUES (?, ?) AS new "
                    "ON DUPLICATE KEY UPDATE `value` = new.`value`",
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
        with self._lock:
            return self._execute(
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

    def recent_turns(self, limit: int) -> list[dict]:
        """Newest first."""
        return self.list_turns(0, limit)

    def list_turns(self, offset: int, limit: int) -> list[dict]:
        """Newest first, paginated."""
        return self._fetchall(
            "SELECT * FROM turns ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
