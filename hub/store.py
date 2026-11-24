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

# 决策路由的默认设置（§5.1）；旧库迁移时逐 key 补上，不覆盖已有值。
DECISION_DEFAULTS = {
    "decision_provider_id": "",
    "decision_enabled": "1",
    "decision_timeout_ms": "3000",
    "decision_gate_len": "40",
    "decision_min_confidence": "0.6",
    "decision_confirm_band": "1",
    "decision_recheck_ratio": "0.05",
    "learn_gen_provider_id": "",
    "learn_max_samples": "200",
}

# settings 表的用途说明（key → 描述，含取值范围）。启动时回填到 settings.description
# （只填空值，不覆盖已有内容）；/admin 总览按它渲染每个配置的提示。
SETTING_DESCRIPTIONS = {
    "current_provider_id": "当前回答模型（providers.id；失效时自动回退到 id 最小的模型）",
    "fallback_provider_id": "设备直连兜底模型（providers.id）：hub 不在线时设备直连用的模型",
    "search_provider_id": "联网搜索模型（providers.id）：执行检索规划与工具调用",
    "current_persona_id": "当前性格/音色（personas.id）",
    "context_gap_seconds": "上下文间隔：距上一条对话超过该秒数即视为新话题、不带上文（正整数，默认 300）",
    "context_max_turns": "上下文最大轮数：最多带多少轮历史（正整数，默认 10）",
    "base_prompt": "基础提示词：拼接在所有性格提示词之前，共同构成 system prompt",
    "device_token": "设备口令（X-Device-Token）：设备直连兜底与学习接口的身份校验",
    "decision_provider_id": "决策模型（providers.id，指向百炼 decision-model-preview 行）；空表示未配置、跳过决策",
    "decision_enabled": "决策路由总开关（0=关闭，1=开启；关闭时未命中正则的文本直接走问答）",
    "decision_timeout_ms": "决策两阶段判定合计超时预算（毫秒，正整数；超时按问答处理）",
    "decision_gate_len": "决策长度闸门：规范化后的文本超过该字符数不调决策模型、直接问答（正整数）",
    "decision_min_confidence": "决策最低置信度（0–1；低于该值按问答处理）",
    "decision_confirm_band": "低置信确认带（0/1）：置信度落在 [min_confidence, 0.75) 时播确认不执行；0 则一律按 min_confidence 直接执行",
    "decision_recheck_ratio": "规则命中后抽样复核比例（0–1；用于发现规则误命中并自动停用）",
    "learn_gen_provider_id": "学习生成规则用的模型（providers.id）；空则回退到当前回答模型",
    "learn_max_samples": "单次学习最多取多少条样本（正整数）",
}

# 三个封闭意图的 key/param_kind 归代码（hub/intents.py 是唯一真源）；这里不再播种，
# 由 sync_intents 在 _init_schema 里调用。文案默认值也以 intents.py 为准。
# manual 规则等价于旧 hub/commands.py 的两条正则（见 spec §5.1）；只写句式不写实体名。
SEED_RULES = [
    ("reset_context", r"^(?:忘掉刚才的对话|忘记刚才的对话|换个话题|重新开始)$", 16, "manual", 100),
    ("switch_target", r"^(?:切换到|切换成|换成|改成|换到|用)(?P<target>.+?)(?:模式|性格|风格|模型|的)?$", 24, "manual", 100),
]

# 只登记已实现的能力；新增能力必须同时注册 handler（见 hub/intents.py）。
SEED_CAPABILITIES = [
    ("切换角色或音色", "换一种说话风格或音色", 10),
    ("切换回答模型", "换成另一个大模型来回答", 20),
    ("忘掉刚才的对话", "重新开始、不接上文", 30),
]

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
    value TEXT,
    description TEXT NOT NULL DEFAULT ''
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
    web_sources TEXT NOT NULL DEFAULT '',
    route TEXT NOT NULL DEFAULT '',
    intent_key TEXT NOT NULL DEFAULT '',
    rule_id INTEGER,
    decision TEXT NOT NULL DEFAULT '',
    learn_state TEXT NOT NULL DEFAULT '',
    corrected INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS turns_ts ON turns (ts);
CREATE TABLE IF NOT EXISTS intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    `key` TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    param_kind TEXT NOT NULL DEFAULT 'none',
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS decision_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_key TEXT NOT NULL DEFAULT '',
    pattern TEXT NOT NULL DEFAULT '',
    max_text_len INTEGER NOT NULL DEFAULT 40,
    source TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'active',
    priority INTEGER NOT NULL DEFAULT 0,
    hit_count INTEGER NOT NULL DEFAULT 0,
    recheck_count INTEGER NOT NULL DEFAULT 0,
    mismatch_count INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_ts REAL NOT NULL DEFAULT 0,
    last_hit_ts REAL
);
CREATE TABLE IF NOT EXISTS capabilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    sort INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1
);
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
        description VARCHAR(512) NOT NULL DEFAULT '',
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
        route VARCHAR(16) NOT NULL DEFAULT '',
        intent_key VARCHAR(64) NOT NULL DEFAULT '',
        rule_id BIGINT NULL,
        decision TEXT NULL,
        learn_state VARCHAR(16) NOT NULL DEFAULT '',
        corrected TINYINT NOT NULL DEFAULT 0,
        PRIMARY KEY (id),
        KEY idx_turns_ts (ts),
        KEY idx_turns_learn_state_ts (learn_state, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS intents (
        id BIGINT NOT NULL AUTO_INCREMENT,
        `key` VARCHAR(64) NOT NULL,
        name VARCHAR(255) NOT NULL DEFAULT '',
        instructions TEXT NOT NULL,
        param_kind VARCHAR(16) NOT NULL DEFAULT 'none',
        enabled TINYINT NOT NULL DEFAULT 1,
        priority INT NOT NULL DEFAULT 0,
        PRIMARY KEY (id),
        UNIQUE KEY uq_intents_key (`key`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS decision_rules (
        id BIGINT NOT NULL AUTO_INCREMENT,
        intent_key VARCHAR(64) NOT NULL DEFAULT '',
        pattern TEXT NOT NULL,
        max_text_len INT NOT NULL DEFAULT 40,
        source VARCHAR(16) NOT NULL DEFAULT 'manual',
        status VARCHAR(16) NOT NULL DEFAULT 'active',
        priority INT NOT NULL DEFAULT 0,
        hit_count INT NOT NULL DEFAULT 0,
        recheck_count INT NOT NULL DEFAULT 0,
        mismatch_count INT NOT NULL DEFAULT 0,
        note TEXT NOT NULL,
        created_ts DOUBLE NOT NULL DEFAULT 0,
        last_hit_ts DOUBLE NULL,
        PRIMARY KEY (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS capabilities (
        id BIGINT NOT NULL AUTO_INCREMENT,
        name VARCHAR(255) NOT NULL DEFAULT '',
        description TEXT NOT NULL,
        sort INT NOT NULL DEFAULT 0,
        enabled TINYINT NOT NULL DEFAULT 1,
        PRIMARY KEY (id)
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
            self._ensure_defaults(DECISION_DEFAULTS)
            self._sync_descriptions()
            self._seed_intents()
            self._seed_rules()
            self._seed_capabilities()

    def _ensure_defaults(self, defaults: dict[str, str]) -> None:
        """按 key 补默认设置（旧库缺的 key 也要补，不覆盖已有值）。"""
        for key, value in defaults.items():
            if self._dialect == "sqlite":
                self._execute(
                    "INSERT OR IGNORE INTO settings (`key`, `value`) VALUES (?, ?)",
                    (key, value),
                )
            else:
                self._execute(
                    "INSERT IGNORE INTO settings (`key`, `value`) VALUES (?, ?)",
                    (key, value),
                )

    def _sync_descriptions(self) -> None:
        """把 SETTING_DESCRIPTIONS 回填到 settings.description，只填空值、不覆盖已有。"""
        for key, description in SETTING_DESCRIPTIONS.items():
            self._execute(
                "UPDATE settings SET description = ? "
                "WHERE `key` = ? AND (description IS NULL OR description = '')",
                (description, key),
            )

    def _seed_intents(self) -> None:
        from hub.intents import sync_intents

        sync_intents(self)

    def _seed_rules(self) -> None:
        # 只在规则表完全为空时播种两条 manual 规则：后台删掉的规则不能在重启后复活，
        # 编辑过的规则也不能被覆盖。按 pattern 判重的旧逻辑会让「删一条重启又回来」。
        existing = self._fetchone("SELECT COUNT(*) AS n FROM decision_rules")
        if existing and existing["n"] > 0:
            return
        for intent_key, pattern, max_text_len, source, priority in SEED_RULES:
            self._execute(
                "INSERT INTO decision_rules (intent_key, pattern, max_text_len, source, "
                "priority, note, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (intent_key, pattern, max_text_len, source, priority, "", time.time()),
            )

    def _seed_capabilities(self) -> None:
        for name, description, sort in SEED_CAPABILITIES:
            exists = self._fetchone(
                "SELECT id FROM capabilities WHERE name = ?", (name,)
            )
            if exists is None:
                self._execute(
                    "INSERT INTO capabilities (name, description, sort) VALUES (?, ?, ?)",
                    (name, description, sort),
                )

    def _migrate_sqlite(self) -> None:
        """给旧库补新列（CREATE TABLE IF NOT EXISTS 不会改已有表）。旧行的新列为 NULL。"""
        cur = self._conn.cursor()
        cur.execute("PRAGMA table_info(settings)")
        scolumns = {row["name"] for row in cur.fetchall()}
        cur.close()
        if "description" not in scolumns:
            self._execute("ALTER TABLE settings ADD COLUMN description TEXT NOT NULL DEFAULT ''")
            log.info("settings: added column description")
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
        # turns 新增六列（旧库补齐；新库建表已含这些列，此处 no-op）。
        cur = self._conn.cursor()
        cur.execute("PRAGMA table_info(turns)")
        tcols = {row["name"] for row in cur.fetchall()}
        cur.close()
        for col, ddl in (
            ("route", "TEXT NOT NULL DEFAULT ''"),
            ("intent_key", "TEXT NOT NULL DEFAULT ''"),
            ("rule_id", "INTEGER"),
            ("decision", "TEXT NOT NULL DEFAULT ''"),
            ("learn_state", "TEXT NOT NULL DEFAULT ''"),
            ("corrected", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in tcols:
                self._execute(f"ALTER TABLE turns ADD COLUMN {col} {ddl}")
                log.info("turns: added column %s", col)
        # 学习索引放在迁移之后建：旧库的 turns 表可能还没有 learn_state 列。
        self._execute(
            "CREATE INDEX IF NOT EXISTS turns_learn_state_ts ON turns (learn_state, ts)"
        )

    def _migrate_mysql(self) -> None:
        row = self._fetchone(
            "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'settings' "
            "AND COLUMN_NAME = 'description'"
        )
        if not row or row["n"] == 0:
            self._execute(
                "ALTER TABLE settings ADD COLUMN description VARCHAR(512) NOT NULL DEFAULT ''"
            )
            log.info("settings: added column description")
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
        for column, ddl in (
            ("route", "VARCHAR(16) NOT NULL DEFAULT ''"),
            ("intent_key", "VARCHAR(64) NOT NULL DEFAULT ''"),
            ("rule_id", "BIGINT NULL"),
            ("decision", "TEXT NULL"),
            ("learn_state", "VARCHAR(16) NOT NULL DEFAULT ''"),
            ("corrected", "TINYINT NOT NULL DEFAULT 0"),
        ):
            row = self._fetchone(
                "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
                f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'turns' AND COLUMN_NAME = '{column}'"
            )
            if not row or row["n"] == 0:
                self._execute(f"ALTER TABLE turns ADD COLUMN {column} {ddl}")
                log.info("turns: added column %s", column)
        # 学习索引：列补齐之后才能建（与 SQLite 侧一致）。
        idx = self._fetchone(
            "SELECT COUNT(*) AS n FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'turns' "
            "AND INDEX_NAME = 'idx_turns_learn_state_ts'"
        )
        if not idx or idx["n"] == 0:
            self._execute(
                "CREATE INDEX idx_turns_learn_state_ts ON turns (learn_state, ts)"
            )

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

    def get_setting_description(self, key: str, default: str = "") -> str:
        row = self._fetchone("SELECT description FROM settings WHERE `key` = ?", (key,))
        return row["description"] if row else default

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
        route: str = "",
        intent_key: str = "",
        rule_id: int | None = None,
        decision: str = "",
        learn_state: str = "",
        corrected: bool = False,
    ) -> int:
        if kind not in TURN_KINDS:
            raise ValueError(f"invalid turn kind: {kind!r}")
        with self._lock:
            return self._execute(
                "INSERT INTO turns (ts, user_text, answer, kind, interrupted, "
                "provider_name, persona_name, web_sources, route, intent_key, rule_id, "
                "decision, learn_state, corrected) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time() if ts is None else ts,
                    user_text,
                    answer,
                    kind,
                    1 if interrupted else 0,
                    provider_name,
                    persona_name,
                    web_sources,
                    route,
                    intent_key,
                    rule_id,
                    decision,
                    learn_state,
                    1 if corrected else 0,
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

    # --- intents ---------------------------------------------------------

    def list_intents(self) -> list[dict]:
        return self._fetchall("SELECT * FROM intents ORDER BY priority, id")

    def get_intent(self, key: str) -> dict | None:
        return self._fetchone("SELECT * FROM intents WHERE `key` = ?", (key,))

    def upsert_intent(self, data: dict) -> int:
        row = self._fetchone("SELECT id FROM intents WHERE `key` = ?", (data.get("key"),))
        if row is not None:
            self._execute(
                "UPDATE intents SET name = ?, instructions = ?, param_kind = ?, enabled = ?, "
                "priority = ? WHERE `key` = ?",
                (
                    data.get("name", ""), data.get("instructions", ""),
                    data.get("param_kind", "none"), 1 if data.get("enabled", 1) else 0,
                    data.get("priority", 0), data.get("key"),
                ),
            )
            return row["id"]
        return self._execute(
            "INSERT INTO intents (`key`, name, instructions, param_kind, enabled, priority) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                data.get("key"), data.get("name", ""), data.get("instructions", ""),
                data.get("param_kind", "none"), 1 if data.get("enabled", 1) else 0,
                data.get("priority", 0),
            ),
        )

    # --- decision rules --------------------------------------------------

    RULE_FIELDS = (
        "intent_key", "pattern", "max_text_len", "source", "status", "priority",
        "hit_count", "recheck_count", "mismatch_count", "note", "last_hit_ts",
    )

    def list_rules(self, status: str | None = None) -> list[dict]:
        if status:
            return self._fetchall(
                "SELECT * FROM decision_rules WHERE status = ? ORDER BY priority DESC, id ASC",
                (status,),
            )
        return self._fetchall("SELECT * FROM decision_rules ORDER BY priority DESC, id ASC")

    def get_rule(self, id: int) -> dict | None:
        return self._get("decision_rules", id)

    def upsert_rule(self, data: dict) -> int:
        return self._upsert("decision_rules", self.RULE_FIELDS, data)

    def delete_rule(self, id: int) -> None:
        self._delete("decision_rules", id)

    def bump_rule_stats(
        self,
        rule_id: int,
        hit: bool = False,
        recheck: bool = False,
        mismatch: bool = False,
    ) -> None:
        with self._lock:
            self._execute(
                "UPDATE decision_rules SET hit_count = hit_count + ?, "
                "recheck_count = recheck_count + ?, mismatch_count = mismatch_count + ?, "
                "last_hit_ts = CASE WHEN ? THEN ? ELSE last_hit_ts END WHERE id = ?",
                (
                    1 if hit else 0,
                    1 if recheck else 0,
                    1 if mismatch else 0,
                    1 if hit else 0,
                    time.time(),
                    rule_id,
                ),
            )

    # --- capabilities ----------------------------------------------------

    def list_capabilities(self) -> list[dict]:
        return self._fetchall("SELECT * FROM capabilities ORDER BY sort, id")

    # --- turn learning state ---------------------------------------------

    def set_turn_learn_state(self, ids: list[int], state: str) -> None:
        if not ids:
            return
        marks = ", ".join("?" for _ in ids)
        self._execute(
            f"UPDATE turns SET learn_state = ? WHERE id IN ({marks})", (state, *ids)
        )

    def set_turn_corrected(self, turn_id: int) -> None:
        self._execute("UPDATE turns SET corrected = 1 WHERE id = ?", (turn_id,))

    def turns_for_learning(self, limit: int) -> list[dict]:
        """learn_state='new' 的最近样本（学习任务用）。"""
        return self._fetchall(
            "SELECT * FROM turns WHERE learn_state = 'new' ORDER BY ts DESC, id DESC LIMIT ?",
            (limit,),
        )

    def recent_qa_texts(self, since_ts: float, limit: int) -> list[str]:
        """最近一段时间内被判成 qa 的文本（学习任务的负例全集）。"""
        rows = self._fetchall(
            "SELECT user_text FROM turns WHERE route = 'model' AND intent_key = 'qa' "
            "AND ts >= ? ORDER BY ts DESC LIMIT ?",
            (since_ts, limit),
        )
        return [r["user_text"] for r in rows if r.get("user_text")]
