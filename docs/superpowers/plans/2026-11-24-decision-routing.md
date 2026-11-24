# 决策路由与规则自学习 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 hub 的命令识别从两条硬编码正则改成「正则表 → 百炼 decision-model-preview → 普通问答」三级路由，并用离线学习任务把决策模型判过、正则没接住的样本蒸馏成新规则入库。

**Architecture:** 新增 `hub/intents.py`（封闭的意图集合与执行器）、`hub/rules.py`（正则表加载/匹配/实体解析）、`hub/decision.py`（systemone 声明式判定客户端与两阶段门面）、`hub/learn.py`（样本挖掘、候选规则校验、入库）。`hub/chat.py` 的 `answer()` 前面插一个 `_route()` 阶段，`hub/store.py` 承载三张新表与 `turns` 扩列。`hub/commands.py` 被取代并删除。

**Tech Stack:** Python 3.12、FastAPI、httpx、sqlite3/pymysql、unittest。

**Spec:** `docs/superpowers/specs/2026-11-24-decision-routing-design.md`

## Global Constraints

- 测试用 `unittest`，不用 pytest。单模块运行：`python -m unittest tests.hub.test_rules -v`；全量回归：`sh scripts/run_tests.sh`。
- `tests/hub/` **不得访问真实网络**：决策模型与生成模型一律用 `httpx.MockTransport` 或假对象；建库用 `tempfile` 的 SQLite 文件。
- 所有 SQL 同时兼容 SQLite 与 MySQL：占位符沿用 `store._sql()`（`?` → `%s`），MySQL 的 `TEXT` 列不能带 `DEFAULT`。
- 所有建表/播种/迁移必须幂等：重复启动不新增行、不覆盖后台改过的文案。
- API key 只放请求头，任何日志与异常消息不得出现 key（沿用 `hub/llm.py` 的脱敏做法）。
- 设备端 `device/` 与 `server/` 零改动。
- 播报文案用纯文本口语，不带 Markdown。
- 提交信息用仓库现有风格：`新增(hub)：…` / `修复(hub)：…` / `docs: …`。

## Review Focus

以下五类输入/失败模式是 spec 隐含但容易漏测的，各自都要在对应任务里钉一条测试：

1. 决策模型返回的 `choice` 不在本次 `criteria` 的 key 里，或 `probabilities` 缺失/为空 → 必须当「未命中」，不能 `KeyError`（Task 6）。
2. 规则意图是 `param_kind='entity'` 但 `pattern` 里没有 `(?P<target>…)` 分组 → 必须当未命中，不能抛 `IndexError`（Task 3）。
3. 输入经 `normalize` 后是空串（只有标点、只有「小爱同学」）→ 直接问答，任何规则都不能匹配空串（Task 3）。
4. 超长输入（例如设备误传一整段日志）→ 长度闸门必须同时挡住规则与决策调用，不得拼进模型请求（Task 7）。
5. 学习任务面对空语料、或全部候选都被拒时 → 返回全零报告，不抛异常、不写入任何规则（Task 10）。

---

### Task 1: 存储层——新表、`turns` 扩列、新默认设置

**Files:**
- Modify: `hub/store.py`
- Test: `tests/hub/test_store.py`

**Interfaces:**
- Consumes: 现有 `Store._execute/_fetchall/_fetchone/_upsert` 与 `SCHEMA_SQLITE`/`SCHEMA_MYSQL`。
- Produces:
  - 表 `intents(id, key, name, instructions, param_kind, enabled, priority)`，`key` 唯一
  - 表 `decision_rules(id, intent_key, pattern, max_text_len, source, status, priority, hit_count, recheck_count, mismatch_count, note, created_ts, last_hit_ts)`
  - 表 `capabilities(id, name, description, sort, enabled)`
  - `turns` 新列：`route TEXT ''`、`intent_key TEXT ''`、`rule_id INTEGER NULL`、`decision TEXT ''`、`learn_state TEXT ''`、`corrected INTEGER 0`；索引 `(learn_state, ts)`
  - `add_turn(..., route="", intent_key="", rule_id=None, decision="", learn_state="", corrected=False)` 增加六个关键字参数并原样写入
  - `list_intents() -> list[dict]`、`upsert_intent(data: dict) -> int`、`get_intent(key: str) -> dict | None`
  - `list_rules(status: str | None = None) -> list[dict]`、`get_rule(id: int) -> dict | None`、`upsert_rule(data: dict) -> int`、`delete_rule(id: int) -> None`
  - `list_capabilities() -> list[dict]`
  - `set_turn_learn_state(ids: list[int], state: str) -> None`、`set_turn_corrected(turn_id: int) -> None`
  - `bump_rule_stats(rule_id: int, hit: bool = False, recheck: bool = False, mismatch: bool = False) -> None`

- [ ] **Step 1: 写失败的测试**（追加到 `tests/hub/test_store.py`）

```python
def test_new_turn_fields_roundtrip(self):
    store = Store(os.path.join(self.tmp, "a.db"))
    store.add_turn("切换成毒舌", "好的，已切换为毒舌", "DeepSeek", "毒舌", "command",
                   route="rule", intent_key="switch_target", rule_id=7,
                   decision='{"confidence": 1.0}', learn_state="new")
    turn = store.list_turns(0, 1)[0]
    self.assertEqual((turn["route"], turn["intent_key"], turn["rule_id"], turn["learn_state"]),
                     ("rule", "switch_target", 7, "new"))
    self.assertEqual(turn["decision"], '{"confidence": 1.0}')
    self.assertEqual(turn["corrected"], 0)

def test_migrates_old_turns_table(self):
    # setUp 里 self.tmp = tempfile.TemporaryDirectory()，self.path = tmp/a.db
    conn = sqlite3.connect(self.path)
    conn.executescript(OLD_SCHEMA)   # 本测试文件内的旧版 DDL 常量，字段与改造前一致
    conn.commit(); conn.close()
    Store(self.path).close()
    conn = sqlite3.connect(self.path)
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(turns)")}
    conn.close()
    self.assertTrue({"route", "intent_key", "rule_id", "decision", "learn_state",
                     "corrected"} <= cols)

def test_seed_creates_intents_rules_capabilities(self):
    store = Store(self.path)
    self.assertEqual([i["key"] for i in store.list_intents()],
                     ["switch_target", "reset_context", "list_capabilities"])
    self.assertEqual([(r["intent_key"], r["source"]) for r in store.list_rules()],
                     [("reset_context", "manual"), ("switch_target", "manual")])
    self.assertEqual(len(store.list_capabilities()), 3)

def test_defaults_added_to_existing_db(self):
    # 旧库里 settings 已有值：不能被 _seed 覆盖，但新 key 要补上
    self.assertEqual(Store(self.old_path).get_setting("decision_timeout_ms"), "800")
    self.assertEqual(Store(self.old_path).get_setting("current_persona_id"), "3")

def test_bump_rule_stats(self):
    store = Store(self.path)
    rid = store.list_rules()[0]["id"]
    store.bump_rule_stats(rid, hit=True, recheck=True, mismatch=True)
    row = store.get_rule(rid)
    self.assertEqual((row["hit_count"], row["recheck_count"], row["mismatch_count"]), (1, 1, 1))
    self.assertIsNotNone(row["last_hit_ts"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.hub.test_store -v`
Expected: FAIL，`TypeError: add_turn() got an unexpected keyword argument 'route'` 等

- [ ] **Step 3: 实现**

- 两个 `SCHEMA_*` 常量里加三张新表的 DDL；`SCHEMA_SQLITE` 的 `turns` 直接带新列；`SCHEMA_MYSQL` 的 `turns` 同理，但 `decision` 用 `TEXT NULL`（MySQL 的 TEXT 不能带 DEFAULT），读回时 `row["decision"] or ""`。
- `_migrate_sqlite` / `_migrate_mysql` 各加六个列的补齐判断，沿用现有的 `information_schema.COLUMNS` / `PRAGMA table_info` 写法；新索引用 `CREATE INDEX IF NOT EXISTS`（SQLite）/ `information_schema.STATISTICS` 判断（MySQL）。
- 新增 `_ensure_defaults(settings: dict)`：逐 key `INSERT ... WHERE NOT EXISTS`（SQLite 用 `INSERT OR IGNORE` 语义、MySQL 用 `INSERT IGNORE`），在 `_init_schema` 里 `_seed()` 之后调用，所以旧库也能补上新 key。默认值照抄 spec §5.1 的表：`decision_provider_id=""`、`decision_enabled="1"`、`decision_timeout_ms="800"`、`decision_gate_len="40"`、`decision_min_confidence="0.6"`、`decision_confirm_band="1"`、`decision_recheck_ratio="0.05"`、`learn_gen_provider_id=""`、`learn_max_samples="200"`。
- 同时播种两个 manual 规则（按 `pattern` 判重）、三条能力（切换角色或音色 / 切换回答模型 / 忘掉刚才的对话）、三行意图（key 与 `hub/intents.py` 一致，本任务先写字面量，Task 2 改为从常量播种）。
- `bump_rule_stats` 用一条 `UPDATE ... SET hit_count = hit_count + ?` 完成，`last_hit_ts` 只在 `hit=True` 时更新。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.hub.test_store -v`
Expected: PASS（含原有全部旧用例）

- [ ] **Step 5: 提交**

```bash
git add hub/store.py tests/hub/test_store.py
git commit -m "新增(hub)：决策规则、意图与能力表，turns 增加路由列"
```

---

### Task 2: `hub/intents.py`——封闭意图集与执行器

**Files:**
- Create: `hub/intents.py`
- Modify: `hub/store.py`（`_seed` 改为调用 `sync_intents`）
- Test: `tests/hub/test_intents.py`

**Interfaces:**
- Consumes: `Store.set_setting`、`store.get_persona`、`store.list_capabilities`、Task 1 的 `list_intents/upsert_intent`。
- Produces:
  - `INTENTS: tuple[dict, ...]`，每项 `{"key": str, "name": str, "instructions": str, "param_kind": "entity"|"none"}`，key 顺序 `switch_target` / `reset_context` / `list_capabilities`
  - `QA_LABEL = "qa"`
  - `INTENT_KEYS: tuple[str, ...]`
  - `sync_intents(store: Store) -> None`
  - `execute(store: Store, intent_key: str, params: dict) -> tuple[str, str]`，返回 `(播报文本, turns.kind)`；`kind` ∈ `command` / `reset`

- [ ] **Step 1: 写失败的测试**

```python
def test_every_intent_has_handler(self):
    self.assertEqual(set(INTENT_KEYS), set(HANDLERS))

def test_sync_intents_does_not_overwrite_edited_text(self):
    store = Store(self.path)
    store.upsert_intent({"key": "reset_context", "name": "重置", "instructions": "后台改过",
                         "param_kind": "none", "enabled": 1, "priority": 2})
    sync_intents(store)
    self.assertEqual(store.get_intent("reset_context")["instructions"], "后台改过")

def test_execute_switch_target(self):
    store = Store(self.path)
    pid = store.list_personas()[2]["id"]
    reply, kind = execute(store, "switch_target", {"kind": "persona", "id": pid, "name": "毒舌"})
    self.assertEqual((reply, kind), ("好的，已切换为毒舌", "command"))
    self.assertEqual(store.current_persona()["name"], "毒舌")

def test_execute_reset_context(self):
    self.assertEqual(execute(store, "reset_context", {}), ("好的，我们重新开始", "reset"))

def test_execute_list_capabilities_skips_disabled(self):
    store = Store(self.path)
    store._execute("UPDATE capabilities SET enabled = 0 WHERE name = '切换回答模型'")
    reply, kind = execute(store, "list_capabilities", {})
    self.assertIn("切换角色或音色", reply)
    self.assertNotIn("切换回答模型", reply)
    self.assertEqual(kind, "command")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.hub.test_intents -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'hub.intents'`

- [ ] **Step 3: 实现**

- `INTENTS` 三条：`switch_target`（`param_kind="entity"`，instructions 写「要求换角色、换音色或换回答用的模型」）、`reset_context`（「要求忘掉之前的对话、重新开始、换个话题」）、`list_capabilities`（「询问助手自己会什么、有哪些技能」）。
- `HANDLERS: dict[str, Callable[[Store, dict], tuple[str, str]]]` 三个函数：`_switch_target`（`params["kind"] == "persona"` 写 `current_persona_id`，`provider` 写 `current_provider_id`；回复文案与现有 `chat._run_command` 一致）、`_reset_context`、`_list_capabilities`（把启用能力 `name` 用「、」连接，句式「我可以帮你切换角色或音色、切换回答模型、忘掉刚才的对话，还能回答问题和联网查资料。」）。
- `sync_intents`：查 `get_intent(key)`，不存在则插入；存在则只在 `instructions` 为空时补默认值，`name`/`param_kind` 同步，`enabled`/`priority` 不动。
- `store._seed` 里的意图播种改为 `sync_intents(self)`（Task 1 的字面量删除）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.hub.test_intents tests.hub.test_store -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add hub/intents.py hub/store.py tests/hub/test_intents.py
git commit -m "新增(hub)：意图注册表与执行器"
```

---

### Task 3: `hub/rules.py`——正则表匹配与实体解析

**Files:**
- Create: `hub/rules.py`
- Modify: `hub/store.py`（无需改动，若 `list_rules` 排序不足则补 `ORDER BY priority DESC, id ASC`）
- Test: `tests/hub/test_rules.py`（新建；用例从 `tests/hub/test_commands.py` 移植）

**Interfaces:**
- Consumes: `store.list_rules(status="active")`、`store.list_personas()`、`store.list_providers()`。
- Produces:
  - `PUNCT`、`WAKE_PREFIX` 常量（从 `hub/commands.py` 迁来）
  - `normalize(text: str) -> str`
  - `@dataclass Rule(id, intent_key, pattern, max_text_len, source, status, priority, regex)`
  - `@dataclass RuleHit(rule: Rule, target: str | None)`
  - `class RuleSet: def __init__(self, rows: list[dict]); def match(self, norm: str) -> RuleHit | None`
  - `load_ruleset(store) -> RuleSet`
  - `resolve_entity(store, target: str) -> dict | None`，命中返回 `{"kind": "persona"|"provider", "id": int, "name": str}`，先角色后模型，name/aliases casefold 精确匹配

- [ ] **Step 1: 写失败的测试**

```python
def test_normalize_strips_wake_prefix_and_punct(self):
    self.assertEqual(normalize("小爱同学，切换到毒舌模式。"), "切换到毒舌模式")

def test_manual_rules_seeded(self):
    rs = load_ruleset(store)
    self.assertEqual(rs.match("忘掉刚才的对话").rule.intent_key, "reset_context")
    self.assertEqual(rs.match("切换到毒舌").rule.intent_key, "switch_target")

def test_length_gate_blocks_long_text(self):
    rs = RuleSet([{"id": 1, "intent_key": "switch_target", "pattern": r"用(?P<target>.+)",
                   "max_text_len": 4, "source": "manual", "status": "active", "priority": 100}])
    self.assertIsNone(rs.match("用一下这个功能吧"))   # 8 字 > 4
    self.assertIsNotNone(rs.match("用毒舌"))

def test_unresolvable_target_falls_through(self):
    rs = RuleSet([
        {"id": 1, "intent_key": "switch_target", "pattern": r"换成(?P<target>.+)",
         "max_text_len": 10, "source": "manual", "status": "active", "priority": 100},
        {"id": 2, "intent_key": "reset_context", "pattern": r"换成新的",
         "max_text_len": 10, "source": "learned", "status": "active", "priority": 50},
    ])
    # 规则 1 的 target「新的」解析不到实体 → 继续尝试规则 2
    self.assertEqual(rs.match("换成新的").rule.id, 2)

def test_entity_intent_without_target_group_is_skipped(self):
    rs = RuleSet([{"id": 1, "intent_key": "switch_target", "pattern": r"切换到毒舌",
                   "max_text_len": 10, "source": "manual", "status": "active", "priority": 100}])
    self.assertIsNone(rs.match("切换到毒舌"))

def test_empty_norm_never_matches(self):
    rs = RuleSet([{"id": 1, "intent_key": "reset_context", "pattern": r".*",
                   "max_text_len": 10, "source": "manual", "status": "active", "priority": 100}])
    self.assertIsNone(rs.match(""))

def test_priority_order(self):
    rs = RuleSet([...两个都匹配，priority 100 的先...])
    self.assertEqual(rs.match("...").rule.priority, 100)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.hub.test_rules -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'hub.rules'`

- [ ] **Step 3: 实现**

- `normalize` 逻辑照搬 `hub/commands.py` 的 `_normalize`。
- `RuleSet.__init__` 只 `re.compile` 以 `^...$` 语义使用的模式（编译失败的行跳过并记 warning，不影响其它规则）；`match` 先判 `len(norm) <= max_text_len`，再 `regex.fullmatch(norm)`。
- 匹配后：意图 `param_kind == "entity"` 时取 `m.groupdict().get("target")`；取不到或 `resolve_entity` 返回 `None` → 继续下一条规则；`param_kind == "none"` 时 `target=None`。
- 每条规则命中时回写 `store.bump_rule_stats(rule.id, hit=True)`——由调用方（`chat._route`）负责，`RuleSet` 保持纯函数、不碰数据库，便于测试。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.hub.test_rules -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add hub/rules.py tests/hub/test_rules.py
git commit -m "新增(hub)：决策正则表匹配与实体解析"
```

---

### Task 4: 规则路径接进 `answer()`，删除 `hub/commands.py`

**Files:**
- Modify: `hub/chat.py:212-242`（`answer` / `_run_command`）
- Delete: `hub/commands.py`、`tests/hub/test_commands.py`
- Test: `tests/hub/test_chat.py`（新建）

**Interfaces:**
- Consumes: `hub.rules.load_ruleset/normalize/RuleHit`、`hub.intents.execute`。
- Produces:
  - `ChatService._route(text: str) -> Route`（本任务只实现 `rule` 与 `none` 两条分支）
  - `@dataclass Route(route: str = "none", intent_key: str = "", rule_id: int | None = None, params: dict = field(default_factory=dict), decision: dict = field(default_factory=dict))`
  - `ChatService.__init__(..., decision=None)` 新增可注入参数（本任务只占位，Task 7 使用）
  - `_run_intent(route, text, speed, volume)` 取代 `_run_command`

- [ ] **Step 1: 写失败的测试**

```python
def test_rule_path_executes_without_qa(self):
    # service 用假 llm_factory，一旦被调用就 fail
    chunks = run(service.answer("切换到毒舌", 1.0, 1.0))
    self.assertTrue(chunks)
    self.assertEqual(service.store.current_persona()["name"], "毒舌")
    turn = service.store.list_turns(0, 1)[0]
    self.assertEqual((turn["route"], turn["intent_key"], turn["learn_state"]),
                     ("rule", "switch_target", "new"))

def test_unmatched_text_goes_to_qa(self):
    run(service.answer("今天天气怎么样", 1.0, 1.0))
    self.assertEqual(service.store.list_turns(0, 1)[0]["route"], "none")

def test_question_words_no_longer_block_commands(self):
    run(service.answer("换成毒舌吗", 1.0, 1.0))
    self.assertEqual(service.store.current_persona()["name"], "毒舌")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.hub.test_chat -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'tests.hub.test_chat'` 之后的断言失败（当前「换成毒舌吗」被 `_QUESTION_RE` 拦掉）

- [ ] **Step 3: 实现**

- `answer()` 改为：`route = await self._route(text)` → `route.intent_key` 非空则走 `_run_intent`，否则走 `_qa`。
- `_route`：`norm = normalize(text)`；`hit = load_ruleset(self.store).match(norm)`；命中则 `route="rule"`、`intent_key=hit.rule.intent_key`、`rule_id=hit.rule.id`、`params={"target": hit.target}`（`entity` 型已由 `resolve_entity` 解析成 `{"kind","id","name"}`，放在 `params["entity"]`）；未命中 `route="none"`。
- `_run_intent`：`switch_target` 的 `params["entity"]` 直接传给 `intents.execute`；所有 `turns` 写入都带上 `route`/`intent_key`/`rule_id`/`decision=json.dumps(...)`/`learn_state`（`rule`→`new`，`none`→`skipped`）。
- 删除 `hub/commands.py` 与 `tests/hub/test_commands.py`；原有用例里仍有效的（`毒舌是什么意思`、`换成海盗模式`、空串）迁进 `test_rules.py`，断言 `match()` 返回 `None`；「换成毒舌吗」「换成毒舌模式好吗？」改为断言匹配成功。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.hub.test_chat tests.hub.test_rules tests.hub.test_app -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add hub/chat.py tests/hub/test_chat.py tests/hub/test_rules.py
git rm hub/commands.py tests/hub/test_commands.py
git commit -m "新增(hub)：正则表接入 answer 路由，移除硬编码指令解析"
```

---

### Task 5: `hub/decision.py`——systemone 客户端

**Files:**
- Create: `hub/decision.py`
- Test: `tests/hub/test_decision.py`

**Interfaces:**
- Consumes: 无需既有模块。
- Produces:
  - `SYSTEMONE_PATH = "/compatible-mode/v1/systemone"`
  - `class DecisionClient: __init__(base_url: str, api_key: str, model: str, transport=None, timeout: float = 5.0)`
  - `async def ask(self, content: str, questions: dict) -> dict | None`——返回响应里的 `answers` 字典；任何失败（超时、非 2xx、坏 JSON、缺 `answers`）返回 `None`，不抛异常
  - `def choice_of(answers: dict, name: str) -> tuple[str, float, dict] | None`——取 `(choice, confidence, probabilities)`，字段缺失时 `probability` 返回 `{}`

- [ ] **Step 1: 写失败的测试**

```python
def test_request_shape(self):
    captured = {}
    transport = httpx.MockTransport(handler)      # handler 记录 request
    client = DecisionClient("https://x.maas.aliyuncs.com", "sk-abc", "decision-model-preview",
                            transport=transport)
    asyncio.run(client.ask("切换到毒舌", {"intent": {"type": "choice", "instructions": "i",
                                                     "criteria": {"qa": "普通提问"}}}))
    self.assertEqual(captured["url"], "https://x.maas.aliyuncs.com/compatible-mode/v1/systemone")
    self.assertEqual(captured["headers"]["authorization"], "sk-abc")   # 无 Bearer
    self.assertEqual(captured["body"]["model"], "decision-model-preview")
    self.assertEqual(captured["body"]["state"], {"content": "切换到毒舌"})
    self.assertEqual(set(captured["body"]["questions"]["intent"]["criteria"]), {"qa"})

def test_parses_answers(self):
    self.assertEqual(choice_of({"intent": {"type": "choice", "choice": "qa", "confidence": 0.91,
                                           "probabilities": {"qa": 0.91}}}, "intent"),
                     ("qa", 0.91, {"qa": 0.91}))

def test_missing_probabilities_tolerated(self):
    self.assertEqual(choice_of({"intent": {"type": "choice", "choice": "qa", "confidence": 0.9}},
                               "intent"), ("qa", 0.9, {}))

def test_unknown_choice_shape_returns_none(self):
    self.assertIsNone(choice_of({}, "intent"))
    self.assertIsNone(choice_of({"intent": {"choice": 3}}, "intent"))

def test_failures_return_none(self):
    # 三种：MockTransport 抛 httpx.ConnectTimeout / 返回 500 / 返回 b"not json"
    self.assertIsNone(asyncio.run(client.ask("x", {})))

def test_error_log_does_not_contain_key(self):
    with self.assertLogs("hub.decision", level="WARNING") as logs:
        asyncio.run(client.ask("x", {}))
    self.assertNotIn("sk-abc", "\n".join(logs.output))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.hub.test_decision -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'hub.decision'`

- [ ] **Step 3: 实现**

- `ask` 用 `httpx.AsyncClient(transport=..., timeout=...)` 发 `POST`，头 `{"authorization": api_key, "content-type": "application/json"}`（**不加 Bearer**，与官方示例一致），体 `{"model": model, "state": {"content": content}, "questions": questions}`。
- 非 2xx、JSON 解析失败、`answers` 不是 dict → `logger.warning("[DECISION] ...")` 后返回 `None`；错误体里若含 key 先替换成 `***`。
- `choice_of`：`answer = answers.get(name)`；不是 dict、`choice` 不是 str、`confidence` 不是数值 → `None`；`probabilities` 非 dict 时返回 `{}`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.hub.test_decision -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add hub/decision.py tests/hub/test_decision.py
git commit -m "新增(hub)：百炼 decision-model-preview 判定客户端"
```

---

### Task 6: `decide()` 门面——两阶段判定与置信度门限

**Files:**
- Modify: `hub/decision.py`
- Test: `tests/hub/test_decision.py`（追加）

**Interfaces:**
- Consumes: Task 5 的 `DecisionClient.ask/choice_of`、Task 2 的 `INTENTS/QA_LABEL`、Task 3 的 `resolve_entity`。
- Produces:
  - `@dataclass Decision(intent_key: str, confidence: float, probabilities: dict, entity: dict | None, raw: dict)`
  - `async def decide(text: str, store, client: DecisionClient, budget_s: float, min_confidence: float) -> Decision | None`——返回 `None` 表示「不是命令或不确定」；`intent_key == "qa"` 也返回 `None`

- [ ] **Step 1: 写失败的测试**

```python
def test_stage1_qa_returns_none(self):
    client = FakeClient({"intent": {"choice": "qa", "confidence": 0.95}})
    self.assertIsNone(asyncio.run(decide("今天天气怎么样", store, client, 0.8, 0.6)))
    self.assertEqual(client.calls, 1)          # 不追问实体

def test_low_confidence_returns_none(self):
    client = FakeClient({"intent": {"choice": "switch_target", "confidence": 0.4}})
    self.assertIsNone(asyncio.run(decide("切换到银月", store, client, 0.8, 0.6)))

def test_stage2_only_for_entity_intent(self):
    client = FakeClient({"intent": {"choice": "reset_context", "confidence": 0.9}})
    asyncio.run(decide("重新开始", store, client, 0.8, 0.6))
    self.assertEqual(client.calls, 1)

def test_target_must_appear_in_text(self):
    # 模型选了「温柔」，但原话里没有「温柔」→ 视为幻觉
    client = FakeClient({"intent": {"choice": "switch_target", "confidence": 0.9},
                         "target": {"choice": "温柔", "confidence": 0.9}})
    self.assertIsNone(asyncio.run(decide("切换到银月", store, client, 0.8, 0.6)))

def test_returns_resolved_entity(self):
    client = FakeClient({"intent": {"choice": "switch_target", "confidence": 0.9},
                         "target": {"choice": "毒舌", "confidence": 0.9}})
    d = asyncio.run(decide("切换到毒舌吧", store, client, 0.8, 0.6))
    self.assertEqual(d.intent_key, "switch_target")
    self.assertEqual(d.entity["kind"], "persona")

def test_budget_shared_across_stages(self):
    slow = FakeClient(delay=0.3)
    start = time.monotonic()
    asyncio.run(decide("切换到毒舌", store, slow, 0.4, 0.6))
    self.assertLess(time.monotonic() - start, 0.7)   # 两阶段合计不超过预算

def test_choice_outside_criteria_is_ignored(self):
    client = FakeClient({"intent": {"choice": "不存在的意图", "confidence": 0.99}})
    self.assertIsNone(asyncio.run(decide("随便说说", store, client, 0.8, 0.6)))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.hub.test_decision -v`
Expected: FAIL，`ImportError: cannot import name 'decide'`

- [ ] **Step 3: 实现**

- 整段包在 `asyncio.timeout(budget_s)` 里，`TimeoutError` → `None`。
- 阶段一 `questions.intent`：`type="choice"`，`instructions` 固定文案「判断这句话是要求助手执行某个控制动作，还是普通提问」，`criteria = {intent.key: intent.instructions for 启用意图} | {"qa": "普通提问、闲聊、创作、翻译，或没有明确控制动作的话"}`。`choice` 不在 criteria 的 key 里、或 `confidence < min_confidence`、或 `== "qa"` → `None`。
- 阶段二仅当意图 `param_kind == "entity"`：`questions.target`，`criteria` 由 personas（名字 + 别名，说明「角色/音色」）与 providers（名字 + 别名，说明「回答模型」）组成；标签冲突时给 provider 加前缀 `模型:`。选中项必须在 `text` 中作为子串出现（casefold）且 `resolve_entity` 能解析，否则 `None`。
- 无论成功失败都 `logger.info` 一条 `[DECISION] intent=%s conf=%.2f ms=%d`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.hub.test_decision -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add hub/decision.py tests/hub/test_decision.py
git commit -m "新增(hub)：决策模型两阶段判定与置信度门限"
```

---

### Task 7: 决策路径接进 `answer()`，判定结果落库

**Files:**
- Modify: `hub/chat.py`（`_route`、`answer`、`_qa` 的落库参数）
- Test: `tests/hub/test_chat.py`（追加）

**Interfaces:**
- Consumes: Task 6 的 `decide`、Task 1 的 `add_turn` 新参数与 `bump_rule_stats`。
- Produces:
  - `ChatService.__init__(..., decision=None)`：`decision` 是可调用对象，签名同 `decide`；生产默认 `hub.decision.decide`，测试注入假对象
  - `_route` 增加 `model` 分支与长度闸门

- [ ] **Step 1: 写失败的测试**

```python
def test_model_path_executes_command(self):
    service = make_service(decision=FakeDecision("switch_target", confidence=0.9,
                                                 entity={"kind": "persona", "id": pid, "name": "毒舌"}))
    run(service.answer("换成那个会吐槽的", 1.0, 1.0))
    self.assertEqual(service.store.current_persona()["name"], "毒舌")
    turn = service.store.list_turns(0, 1)[0]
    self.assertEqual((turn["route"], turn["intent_key"], turn["learn_state"]),
                     ("model", "switch_target", "new"))
    self.assertIn("0.9", turn["decision"])

def test_timeout_falls_back_to_qa_and_marks_skipped(self):
    service = make_service(decision=RaisingDecision())      # 抛 TimeoutError
    run(service.answer("换成那个会吐槽的", 1.0, 1.0))
    turn = service.store.list_turns(0, 1)[0]
    self.assertEqual((turn["route"], turn["learn_state"]), ("none", "skipped"))

def test_gate_len_skips_decision_call(self):
    spy = SpyDecision()
    service = make_service(decision=spy)
    text = "请帮我详细讲一下这个问题的来龙去脉以及我们应该怎么处理它才好呢顺便也说说有什么风险"
    self.assertGreater(len(normalize(text)), 40)
    run(service.answer(text, 1.0, 1.0))
    self.assertEqual(spy.calls, 0)

def test_decision_said_qa_records_qa_intent(self):
    service = make_service(decision=FakeDecision("qa", confidence=0.95))
    run(service.answer("换成那个会吐槽的", 1.0, 1.0))
    self.assertEqual(service.store.list_turns(0, 1)[0]["intent_key"], "qa")
```

（`test_gate_len_skips_decision_call` 的句子必须在 `normalize` 之后超过 40 字，实现时用一段 45 字的真实问句。）

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.hub.test_chat -v`
Expected: FAIL，模型路径仍走问答、`route` 仍是 `none`

- [ ] **Step 3: 实现**

- `_route` 在规则未命中后：`decision_enabled` 为假 → `none`；`len(norm) > decision_gate_len` → `none`；否则 `await self.decision(norm, self.store, client, budget_s, min_confidence)`。`budget_s = decision_timeout_ms / 1000`，`min_confidence` 从设置读（读不到用默认值，沿用 `_int_setting` 的容错风格，浮点另写一个 `_float_setting`）。
- 决策客户端按 `decision_provider_id` 从 providers 表构造；provider 缺失或 `base_url` 为空 → 记 warning，`route='none'`。
- `route='model'` 且拿到决策 → `params["entity"]`、`intent_key`；`route='model'` 但决策为 `None` → `route='model'` + `intent_key='qa'`（表示模型确实判过）；异常/超时 → `route='none'`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.hub.test_chat -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add hub/chat.py tests/hub/test_chat.py
git commit -m "新增(hub)：决策模型接入路由并记录判定结果"
```

---

### Task 8: 抽样复核、自动停用、纠正检测、确认带

**Files:**
- Modify: `hub/chat.py`
- Test: `tests/hub/test_chat.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `bump_rule_stats` / `set_turn_corrected`、Task 7 的 `_route`。
- Produces:
  - `ChatService._maybe_recheck(norm: str, route: Route) -> None`（fire-and-forget）
  - `ChatService._pending: tuple[float, Route] | None`（确认带状态）
  - `ChatService._last_command: tuple[float, str, dict, int] | None`（纠正检测）

- [ ] **Step 1: 写失败的测试**

```python
def test_confirm_band_asks_without_executing(self):
    service = make_service(decision=FakeDecision("switch_target", confidence=0.7,
                                                 entity={"kind": "persona", "id": pid, "name": "毒舌"}))
    run(service.answer("换成那个会吐槽的", 1.0, 1.0))
    self.assertNotEqual(service.store.current_persona()["name"], "毒舌")
    turn = service.store.list_turns(0, 1)[0]
    self.assertEqual(turn["answer"], "你是要切换到毒舌吗")
    self.assertEqual(turn["intent_key"], "switch_target")

def test_confirm_followup_executes(self):
    service = ...   # 先制造一次待确认，再发「对」
    run(service.answer("对", 1.0, 1.0))
    self.assertEqual(service.store.current_persona()["name"], "毒舌")

def test_confirm_expires_after_30s(self):
    # clock 拨快 31 秒后说「对」→ 不执行，走问答
    self.assertEqual(service.store.current_persona()["name"], "默认")

def test_correction_marks_previous_turn(self):
    run(service.answer("切换到毒舌", 1.0, 1.0))
    run(service.answer("不对，是温柔", 1.0, 1.0))
    self.assertEqual(service.store.list_turns(0, 2)[1]["corrected"], 1)

def test_recheck_mismatch_increments_counters(self):
    # 规则命中后强制复核（ratio=1.0），假决策说 qa
    run(service.answer("切换到毒舌", 1.0, 1.0))
    row = next(r for r in service.store.list_rules() if r["intent_key"] == "switch_target")
    self.assertEqual((row["hit_count"], row["recheck_count"], row["mismatch_count"]), (1, 1, 1))

def test_auto_disable_after_mismatch_ratio(self):
    # 10 次命中全部复核不一致 → 规则 status 变 disabled
    self.assertEqual(service.store.get_rule(rid)["status"], "disabled")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.hub.test_chat -v`
Expected: FAIL，无确认带/复核/纠正逻辑

- [ ] **Step 3: 实现**

- 确认带：`decision_min_confidence <= confidence < 0.75` 且 `decision_confirm_band` 为真 → 不执行，播「你是要切换到{name}吗」，把 `(clock(), route)` 存进 `_pending` 并落库（`intent_key` 已写、`params` 进 `decision`）。下一条若 `normalize` 后匹配 `^(对|是|嗯|好|可以|没错)$` 且距 `_pending` ≤30 秒 → 用 `_pending` 的 route 执行；否则清空 `_pending` 后正常走 `_route`。
- 复核：`route.route == "rule"` 时 `ratio = 0.2 if (rule.source == "learned" and hit_count < 50) else decision_recheck_ratio`，`random.random() < ratio` 才调 `decide`（只比意图）。用 `asyncio.ensure_future` 起任务，失败只记日志。不一致 → `bump_rule_stats(id, recheck=True, mismatch=True)`，一致 → `recheck=True`。
- 自动停用：复核写入后查回该规则，`recheck_count >= 10 and mismatch_count / recheck_count >= 0.25` → `upsert_rule({..., "status": "disabled"})` 并记 warning。
- 纠正检测：命令执行后存 `(clock(), intent_key, params, turn_id)`；下一条进来时，若 ≤60 秒且（同意图不同参数 或 `normalize` 后匹配 `^(不对|不是|我说的是|搞错)`）→ `set_turn_corrected(turn_id)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.hub.test_chat -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add hub/chat.py tests/hub/test_chat.py
git commit -m "新增(hub)：规则复核、自动停用与确认带"
```

---

### Task 9: `hub/learn.py`——候选规则校验

**Files:**
- Create: `hub/learn.py`
- Test: `tests/hub/test_learn.py`

**Interfaces:**
- Consumes: 无（纯函数）。
- Produces:
  - `ALLOWED_PATTERN_CHARS` / `_FORBIDDEN = ("(?=", "(?!", "(?<=", "(?<!", "\\1", "\\2", "\\3")`
  - `def validate_candidate(pattern: str, model_max_len: int | None, positives: list[str], negatives: list[str], existing: list[dict]) -> tuple[int, str | None]`——返回 `(max_text_len, None)` 表示通过，`(0, 原因)` 表示拒绝；原因是 `invalid_regex` / `forbidden_syntax` / `no_cjk` / `too_long` / `low_hit_rate` / `matches_negative` / `duplicate`

- [ ] **Step 1: 写失败的测试**

```python
def test_accepts_generalized_pattern(self):
    maxlen, reason = validate_candidate(r"^(?:把)?角色(?:切换|换)(?:成|到|为)(?P<target>.+)$",
                                        None, ["角色切换到银月", "角色换成毒舌"], ["今天天气怎么样"], [])
    self.assertIsNone(reason)
    self.assertEqual(maxlen, 13)      # 最长命中样本 7 字 + 6，夹在 [8, 40]

def test_rejects_invalid_regex(self):
    self.assertEqual(validate_candidate("([", None, ["a"], [], [])[1], "invalid_regex")

def test_rejects_lookaround(self):
    self.assertEqual(validate_candidate(r"^(?=切换).+$", None, ["切换"], [], [])[1], "forbidden_syntax")

def test_rejects_pattern_without_cjk(self):
    self.assertEqual(validate_candidate(r"^switch$", None, ["switch"], [], [])[1], "no_cjk")

def test_rejects_pattern_matching_negative(self):
    self.assertEqual(validate_candidate(r"^换(?:成|到)(?P<target>.+)$", None,
                                        ["换成毒舌"], ["换成什么好"], [])[1], "matches_negative")

def test_rejects_duplicate_of_existing(self):
    existing = [{"pattern": r"^换成(?P<target>.+)$"}]
    self.assertEqual(validate_candidate(r"^换成(?P<target>.+)$", None,
                                        ["换成毒舌"], [], existing)[1], "duplicate")

def test_rejects_low_hit_rate(self):
    self.assertEqual(validate_candidate(r"^换成(?P<target>毒舌)$", None,
                                        ["换成毒舌", "换成银月", "换个温柔"], [], [])[1], "low_hit_rate")

def test_max_len_clamped(self):
    self.assertEqual(validate_candidate(r"^(?P<target>.+)$", 99, ["毒舌"], [], [])[0], 40)
```

`max_text_len` 的计算规则固定为：`clamp(max(max(len(p) for p in 命中的正例) + 6, model_max_len or 0), 8, 40)`。`test_accepts_generalized_pattern` 的断言按此规则填实际值。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.hub.test_learn -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'hub.learn'`

- [ ] **Step 3: 实现**

- 顺序：长度 ≤200 → 禁止语法 → `re.compile` → 含至少一个 CJK（`一-鿿`）→ 用 `fullmatch` 命中率 ≥50% → 不命中任何负例 → 不与 `existing` 中任意 pattern 字符串相同（归一化空白后比较）。
- `fullmatch` 逐个样本跑；命中样本的 `len()` 用于算 `max_text_len`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.hub.test_learn -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add hub/learn.py tests/hub/test_learn.py
git commit -m "新增(hub)：学到的正则规则安全校验"
```

---

### Task 10: `hub/learn.py`——样本挖掘与入库

**Files:**
- Modify: `hub/learn.py`、`hub/store.py`（补 `turns_for_learning` / `recent_qa_texts`）
- Test: `tests/hub/test_learn.py`（追加）

**Interfaces:**
- Consumes: Task 9 的 `validate_candidate`、`store.turns_for_learning(limit)`、`store.recent_qa_texts(since_ts, limit)`、`store.list_rules`、`store.upsert_rule`、`store.set_turn_learn_state`。
- Produces:
  - `@dataclass LearnReport(scanned: int, proposed: int, accepted: int, rejected: list[dict], added_rule_ids: list[int])`，`rejected` 项为 `{"intent_key": str, "pattern": str, "reason": str}`
  - `async def run_learning(store, generate: Callable[[str, str], Awaitable[str]], max_samples: int = 200) -> LearnReport`——`generate(system, user)` 返回模型输出的 JSON 文本；生产实现用当前 provider 打 `/chat/completions`（非流式、`response_format={"type":"json_object"}`、超时 60 秒），测试注入假对象

- [ ] **Step 1: 写失败的测试**

```python
def test_inserts_active_learned_rule(self):
    store = seed_turns([("角色切换到银月", "model", "switch_target")])
    report = asyncio.run(run_learning(store, fake_gen(r"^(?:把)?角色切换到(?P<target>.+)$")))
    self.assertEqual((report.scanned, report.proposed, report.accepted), (1, 1, 1))
    rule = store.get_rule(report.added_rule_ids[0])
    self.assertEqual((rule["source"], rule["status"], rule["priority"]), ("learned", "active", 50))

def test_second_run_is_idempotent(self):
    asyncio.run(run_learning(store, fake_gen(pattern)))
    second = asyncio.run(run_learning(store, fake_gen(pattern)))
    self.assertEqual(second.scanned, 0)          # 样本已标记 used
    self.assertEqual(len(store.list_rules()), 3)

def test_marks_skipped_vs_used(self):
    # 一条 route='none' 的样本不参与，标 skipped
    self.assertEqual(store.list_turns(0, 1)[0]["learn_state"], "skipped")

def test_empty_corpus_returns_zero_report(self):
    report = asyncio.run(run_learning(Store(self.path), fake_gen("x")))
    self.assertEqual((report.scanned, report.accepted, report.added_rule_ids), (0, 0, []))

def test_provider_failure_returns_none(self):
    self.assertIsNone(asyncio.run(run_learning(store, raising_gen()))   # 模型输出解析失败：不改样本状态
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.hub.test_learn -v`
Expected: FAIL，`ImportError: cannot import name 'run_learning'`

- [ ] **Step 3: 实现**

- 取样本：`learn_state='new'` 最近 `max_samples` 条；正例 `route IN ('rule','model')` 且 `intent_key` 不在 `('', 'qa')` 且 `corrected == 0`；负例 `route='model' AND intent_key='qa'`。
- 负例全集另取：`recent_qa_texts(since_ts=now-30天, limit=500)`，用于校验。
- 按意图分组，每组正例去重后取 30 条、负例取 30 条，连同意图 `instructions`、`param_kind`、实体名与别名、已有 active 规则（≤20 条，含该意图的样例文本），拼成一个 JSON user 消息给 `generate`；system 提示词写死：只输出 `{"rules":[{"intent_key","pattern","max_text_len"}]}`、`fullmatch` 语义、最多一个 `(?P<target>…)`、禁用 lookaround/反向引用/嵌套量词、≤200 字符、必须含中文、不要写死实体名。
- 解析模型输出失败 → 本次不改任何样本状态，返回 `None`（接口层转 500）；逐条过 `validate_candidate`，通过则 `upsert_rule({"intent_key", "pattern", "max_text_len", "source": "learned", "status": "active", "priority": 50, "note": "由学习任务生成"})`。
- 全部处理完：正/负例样本 `set_turn_learn_state(ids, "used")`；`route='none'` 的样本 `"skipped"`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.hub.test_learn -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add hub/learn.py hub/store.py tests/hub/test_learn.py
git commit -m "新增(hub)：学习任务取样本、生成规则并入库"
```

---

### Task 11: 学习 HTTP 接口与后台两页

**Files:**
- Modify: `hub/app.py`（新路由）、`hub/admin.py`（`/rules`、`/intents` 两页）
- Test: `tests/hub/test_app.py`、`tests/hub/test_admin.py`（追加）

**Interfaces:**
- Consumes: Task 10 的 `run_learning`、`store.get_setting("device_token")`、现有 `require_same_origin`。
- Produces:
  - `POST /api/v1/decision/learn`，头 `X-Device-Token`，返回 `{"scanned", "proposed", "accepted", "rejected", "added_rule_ids"}`；口令不符 403
  - `GET/POST /admin/rules`、`GET/POST /admin/intents`

- [ ] **Step 1: 写失败的测试**

```python
def test_learn_endpoint_requires_token(self):
    self.assertEqual(client.post("/api/v1/decision/learn").status_code, 403)

def test_learn_endpoint_returns_report(self):
    r = client.post("/api/v1/decision/learn", headers={"X-Device-Token": token})
    self.assertEqual(r.status_code, 200)
    self.assertEqual(set(r.json()), {"scanned", "proposed", "accepted", "rejected", "added_rule_ids"})

def test_admin_rules_page_lists_rules(self):
    body = client.get("/admin/rules").text
    self.assertIn("switch_target", body)
    self.assertIn("learned", body)

def test_admin_can_disable_rule(self):
    client.post("/admin/rules", data={"id": rid, "status": "disabled", ...})
    self.assertEqual(store.get_rule(rid)["status"], "disabled")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.hub.test_app tests.hub.test_admin -v`
Expected: FAIL，404

- [ ] **Step 3: 实现**

- 复用 `app.py` 里 `device_fallback` 的 `hmac.compare_digest` 校验写法；`run_learning` 返回 `None` → 500。
- `/admin/rules` 表格列：意图、模式、来源、状态、长度上限、命中/复核/不一致、操作（停用/删除）；`/admin/intents` 可改 `instructions`/`enabled`，并列出能力清单的勾选。表单沿用 `admin.py` 现有 `_page`/`_e`/`require_same_origin` 风格。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.hub.test_app tests.hub.test_admin -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add hub/app.py hub/admin.py tests/hub/test_app.py tests/hub/test_admin.py
git commit -m "新增(hub)：学习接口与规则/意图后台"
```

---

### Task 12: 黄金语料、评测脚本、文档

**Files:**
- Create: `tests/hub/fixtures/routing_cases.json`、`scripts/eval_routing.py`
- Modify: `tests/hub/test_rules.py`（语料回归）、`docs/status.md`
- Create: `docs/testing/2026-11-24-decision-routing.md`

**Interfaces:**
- Consumes: 全部前序模块。
- Produces: 语料格式 `[{"text": str, "route": "rule"|"none", "intent_key": str}]`——只有 `rule` 与 `none` 两类进 unittest；`model` 类句子写进文档的「待真机/真实接口验证」清单。

- [ ] **Step 1: 写失败的测试**

```python
def test_golden_cases_route_local(self):
    cases = json.loads(Path(__file__).with_name("fixtures/routing_cases.json").read_text("utf-8"))
    for case in cases:
        if case["route"] not in ("rule", "none"):
            continue
        route = route_of(case["text"])          # 用 decision 关闭的 ChatService._route
        self.assertEqual((route.route, route.intent_key or "qa"),
                         (case["route"], case["intent_key"] or "qa"), case["text"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.hub.test_rules -v`
Expected: FAIL，fixtures 文件不存在

- [ ] **Step 3: 实现**

- 语料至少 30 条：切换类（含「将角色切换到银月」「进入毒舌模式」「切换成DeepSeek」）、重置类、能力类、以及必须判 `none` 的长问句（>40 字）、感叹类、「毒舌是什么意思」。
- `scripts/eval_routing.py`：读同一份语料，构造真实 `DecisionClient`（provider 从 `decision_provider_id` 取），逐条打真实接口，打印 `text | expected | actual | confidence | 耗时`，最后输出准确率；不联网时不进 `tests/`。
- `docs/status.md` 增加「决策路由」小节（三级路由、阈值、学习接口、不覆盖设备直连路径）；`docs/testing/2026-11-24-decision-routing.md` 记录实际跑过的命令与结果，按 `TESTING.md` 的分层验证原则写，不得把文件回放写成真人验收。

- [ ] **Step 4: 跑全量回归**

Run: `sh scripts/run_tests.sh`
Expected: 全部通过，含三条 `--check` 的嵌入一致性与 `sh -n`

- [ ] **Step 5: 提交**

```bash
git add tests/hub/fixtures/routing_cases.json scripts/eval_routing.py tests/hub/test_rules.py docs/status.md docs/testing/2026-11-24-decision-routing.md
git commit -m "docs: 决策路由黄金语料、评测脚本与验收记录"
```

---

## Self-Review

**Spec coverage**

| Spec 章节 | 任务 |
|---|---|
| §5.1 三张表 + `turns` 六列 + 新设置 | Task 1 |
| §5.1 意图 key 归代码、`sync_intents` | Task 2 |
| §5.1 规则表与 manual 播种、实体槽、删 `commands.py` | Task 3、Task 4 |
| §5.2 规则匹配与长度闸门 | Task 3 |
| §5.2 决策两阶段、置信度、实体必须在原话里 | Task 6 |
| §5.2 路由接进 `answer()`、落库、`learn_state` | Task 7 |
| §5.2 确认带、抽样复核、自动停用、纠正检测 | Task 8 |
| §5.3 候选校验（负例全集、`max_text_len`、白名单） | Task 9、Task 10 |
| §5.3 样本挖掘与入库、幂等 | Task 10 |
| §5.4 后台两页 | Task 11 |
| §5.5 学习接口与失败降级 | Task 10、Task 11 |
| §5.6 测试与黄金语料、`status.md`、测试记录 | Task 12 |

**类型一致性**：`Route`（chat）与 `Decision`（decision）字段名一致使用 `intent_key`/`confidence`/`probabilities`/`entity`；`bump_rule_stats` 在三处被调用（Task 3 的调用方 `_route`、Task 8 的复核、Task 8 的自动停用）签名一致；`validate_candidate` 的返回形状 `(int, str|None)` 在 Task 9 与 Task 10 一致。

**Review Focus 落点**：第 1 条 → Task 6 `test_choice_outside_criteria_is_ignored`、`test_missing_probabilities_tolerated`；第 2 条 → Task 3 `test_entity_intent_without_target_group_is_skipped`；第 3 条 → Task 3 `test_empty_norm_never_matches`；第 4 条 → Task 7 `test_gate_len_skips_decision_call`；第 5 条 → Task 10 `test_empty_corpus_returns_zero_report`。

**体量**：12 个任务，每个任务独立可测、可单独回滚；Task 10 依赖 Task 9 的校验函数，其余任务只依赖更早的任务。
