# NAS Hub（模型 + TTS 合并服务）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建独立服务 `hub/`（端口 9999），把模型调用、联网搜索、性格、上下文、TTS 合并到 NAS 端，SQLite 持久化并带网页后台；设备改走 hub，hub 不在线时自动退回直连 DeepSeek。

**Architecture:** FastAPI 单进程服务，所有可变配置只认项目根目录的 SQLite（默认 `hub.db`）。对设备暴露与现有 `server/main.py` 同格式的 `POST /api/v1/stream/text_chat`、`POST /api/v1/tts/stream`（流式 WAV）。复用 `server/streaming_pipeline.StreamingPipeline` 与 `server/tts/edge_tts_client.EdgeTTSClient`；模型调用用自写的 OpenAI 兼容流式客户端（实现 `chat_stream` 接口以适配 pipeline）。设备端只改 `device/native_first_client.sh`。

**Tech Stack:** Python 3.14、FastAPI、uvicorn、httpx、edge-tts（需 ffmpeg）、sqlite3（标准库）、unittest；设备端 BusyBox ash + curl + jsonfilter。

**Spec:** `docs/superpowers/specs/2026-11-22-nas-llm-hub-design.md`

## Global Constraints

- 服务监听 `0.0.0.0:9999`（env `HUB_PORT` 可覆盖）；数据库默认项目根目录 `hub.db`（env `HUB_DB` 可覆盖，测试用临时文件）。
- 配置来源只有 SQLite，不读 `config.yaml`。
- 对设备接口与现有 `server/main.py` 完全同格式：`multipart/form-data` 字段 `message`、`session_id`、`backend`、`speed`、`volume`；返回流式 WAV，32000 Hz、16-bit、单声道，RIFF 头与 `server/main.py` 一致（`max_data = 180 * 32000 * 2`）。hub 忽略 `backend` 和 `session_id`。
- 上下文：按时间判定连续对话，默认间隔 300 秒、最多 10 轮，两值存在设置表、后台可改。
- 语音命令由规则识别，不用模型函数调用；"忘掉刚才的对话""换个话题""重新开始"都是清空上下文。
- 联网搜索固定走 DeepSeek：`https://api.deepseek.com/v1/tools/search`、`/v1/tools/fetch`，垫话 `搜索中，请稍等。`
- API key 在后台只显示后 4 位，任何日志不打印 key。
- 退回配置接口必须校验请求头 `X-Device-Token`。
- 测试用 `unittest`（仓库无 pytest），命令 `python -m unittest ...`；任何测试不得访问真实网络（用 `httpx.MockTransport` 或假对象）。
- 设备端改动只在 `device/native_first_client.sh`，保持 BusyBox ash 兼容（`sh -n` 必须通过）。
- **不得执行 `git commit`/`git push`**（用户要求）：每个任务结束时只保留工作区改动，由用户决定何时提交。
- **执行顺序门槛**（用户要求）：Task 1–9 + "Hub 验收门槛" 全部通过并经用户确认之后，才能开始 Task 10/11（设备相关）。

## Review Focus

1. **客户端中途断开（播报被打断）**：hub 必须停止模型调用并把已生成的部分回答存为 `interrupted=1`，不能抛异常刷屏或丢掉整条记录 → Task 6 `test_disconnect_saves_partial_interrupted`。
2. **模型/搜索接口报错或超时**：在任何音频发出前失败时返回 HTTP 502（设备据此走失败处理），而不是返回 200 + 空 WAV 让设备静默 → Task 6 `test_model_error_before_audio_returns_502`。
3. **当前模型/性格被后台删除**：回答不能崩，回退到 id 最小的一条并记日志 → Task 1 `test_current_falls_back_when_deleted`。
4. **问题里含性格名但不是切换**（"毒舌是什么意思"）：必须当普通问答 → Task 3 `test_question_mentioning_persona_is_not_command`。
5. **设备拉退回配置时 hub 不在线 / 返回非 JSON**：客户端保留 env 原值并正常启动 → Task 10 `test_fallback_fetch_failure_keeps_env`。

---

### Task 1: SQLite 存储层

**Files:**
- Create: `hub/__init__.py`（空）、`hub/store.py`
- Test: `tests/hub/__init__.py`（空）、`tests/hub/test_store.py`

**Interfaces:**
- Produces（`hub/store.py`）：
  - `class Store: __init__(self, path: str)`；首次打开自动建表并写种子数据。
  - 供应商：`list_providers() -> list[dict]`、`get_provider(id: int) -> dict | None`、`upsert_provider(data: dict) -> int`、`delete_provider(id: int) -> None`。dict 键：`id, name, aliases（逗号分隔字符串）, base_url, api_key, model, temperature（float|None）, max_tokens（int|None）`。
  - 性格：`list_personas()`、`get_persona(id)`、`upsert_persona(data) -> int`、`delete_persona(id)`。dict 键：`id, name, aliases, prompt, voice`。
  - 设置：`get_setting(key: str, default: str | None = None) -> str | None`、`set_setting(key: str, value: str) -> None`。键：`current_provider_id`、`current_persona_id`、`fallback_provider_id`、`search_provider_id`、`context_gap_seconds`（默认 `"300"`）、`context_max_turns`（默认 `"10"`）、`base_prompt`、`device_token`。
  - `current_provider() -> dict`、`current_persona() -> dict`：设置指向的记录不存在时回退到 id 最小的一条。
  - 历史：`add_turn(user_text: str, answer: str, provider_name: str, persona_name: str, kind: str, web_sources: str = "", interrupted: bool = False, ts: float | None = None) -> int`，`kind ∈ {"qa", "command", "reset"}`；`recent_turns(limit: int) -> list[dict]`（按时间倒序，dict 含 `id, ts, user_text, answer, kind, interrupted, provider_name, persona_name, web_sources`）；`list_turns(offset: int, limit: int) -> list[dict]`。
- 种子数据：供应商 `DeepSeek`（aliases `deepseek,深度求索`，base_url `https://api.deepseek.com`，model `deepseek-flash`，api_key 取 env `DEEPSEEK_API_KEY`，没有则空串）；性格 `默认`（voice `zh-CN-YunjianNeural`）、`温柔`（`zh-CN-XiaoxiaoNeural`）、`毒舌`（`zh-CN-YunxiNeural`）、`撒娇`（`zh-CN-XiaoyiNeural`），提示词由实现者撰写一句话风格描述；`base_prompt` = `你是一个语音助手，请用纯文本口语直接回答，不要复述问题，不要使用任何 Markdown 格式或列表，说完整的句子。`；`current_*`、`fallback_provider_id`、`search_provider_id` 都指向 DeepSeek；`device_token` 用 `secrets.token_urlsafe(16)` 生成。

- [ ] **Step 1: 写失败测试** `tests/hub/test_store.py`，每个测试用 `tempfile` 新建 db：
  - `test_seed_defaults`：新库 `list_personas()` 名称集合 == `{"默认","温柔","毒舌","撒娇"}`；`get_setting("context_gap_seconds") == "300"`；`current_provider()["name"] == "DeepSeek"`；`len(get_setting("device_token")) >= 16`。
  - `test_seed_only_once`：同一路径打开两次，性格仍为 4 条。
  - `test_persona_roundtrip_and_current`：`upsert_persona({"name":"东北","aliases":"","prompt":"p","voice":"zh-CN-YunxiNeural"})` 后 `set_setting("current_persona_id", str(id))`，`current_persona()["name"] == "东北"`。
  - `test_current_falls_back_when_deleted`：把当前性格删掉，`current_persona()` 返回 id 最小的一条，不抛异常。
  - `test_turns_order_and_flags`：依次 `add_turn(... ts=100)`、`add_turn(... ts=200, interrupted=True)`，`recent_turns(10)[0]["ts"] == 200` 且 `interrupted == 1`。
- [ ] **Step 2:** `python -m unittest tests.hub.test_store -v` → FAIL（`ModuleNotFoundError: hub.store`）。
- [ ] **Step 3:** 实现 `hub/store.py`（`sqlite3`，`check_same_thread=False`，写操作加 `threading.Lock`）。
- [ ] **Step 4:** 同命令 → 全部 PASS。

### Task 2: 上下文选择

**Files:**
- Create: `hub/context.py`
- Test: `tests/hub/test_context.py`

**Interfaces:**
- Consumes：`Store.recent_turns` 返回的 dict 列表（倒序）。
- Produces：`select_context(turns: list[dict], now: float, gap_seconds: int, max_turns: int) -> list[dict]`，返回 OpenAI 格式消息列表（正序，`[{"role":"user","content":...},{"role":"assistant","content":...}, ...]`）。
- 规则：从最新一条往前走；当前时间与最新一条、以及相邻两条之间的间隔都 ≤ `gap_seconds` 才算同一段；遇到 `kind == "reset"` 立即停止（不含它）；`kind == "command"` 的轮次跳过不放入上下文但不打断链；最多取 `max_turns` 轮；`interrupted` 的回答照常放入。

- [ ] **Step 1: 写失败测试**：
  - `test_chain_within_gap`：ts 为 0、200、400，now=500，gap=300 → 3 轮全部返回，首条 content 为 ts=0 的问题。
  - `test_gap_breaks_chain`：ts 为 0、400，now=500 → 只返回 ts=400 那轮。
  - `test_now_too_far_returns_empty`：最新 ts=0，now=301 → `[]`。
  - `test_reset_stops_chain`：ts 100(qa)、200(reset)、300(qa)，now=310 → 只返回 ts=300。
  - `test_commands_skipped`：ts 100(qa)、200(command)、300(qa) → 返回 2 轮。
  - `test_max_turns`：12 轮间隔 10 秒，max_turns=10 → 20 条消息，且是最新的 10 轮。
- [ ] **Step 2:** `python -m unittest tests.hub.test_context -v` → FAIL。
- [ ] **Step 3:** 实现 `select_context`。
- [ ] **Step 4:** 同命令 → PASS。

### Task 3: 语音命令识别

**Files:**
- Create: `hub/commands.py`
- Test: `tests/hub/test_commands.py`

**Interfaces:**
- Produces：
  - `@dataclass class Command: kind: str; target_id: int | None; target_name: str | None`，`kind ∈ {"persona", "provider", "reset"}`。
  - `parse_command(text: str, personas: list[dict], providers: list[dict]) -> Command | None`。
- 规则（实现者可扩充句式，但下列测试必须通过）：先去掉首尾标点空白及前缀"小爱同学"；reset 句式整句匹配 `忘掉刚才的对话|忘记刚才的对话|换个话题|重新开始`；切换句式为 `(切换到|切换成|换成|改成|换到|用)` + 名称/别名 + 可选后缀 `(模式|性格|风格|模型|的)?`，且整句不得含疑问词（`吗|什么|怎么|为什么|是谁|？|?`）；名称匹配不区分大小写，性格先于供应商。

- [ ] **Step 1: 写失败测试**（性格用 Task 1 种子名，供应商 `[{"id":1,"name":"DeepSeek","aliases":"deepseek"},{"id":2,"name":"Kimi","aliases":"kimi,月之暗面"}]`）：
  - `test_switch_persona`：`"换成毒舌模式"` → `Command("persona", <毒舌 id>, "毒舌")`；`"小爱同学，切换到温柔"` 同理为温柔。
  - `test_switch_provider_case_insensitive`：`"切换到kimi"` → `Command("provider", 2, "Kimi")`；`"换成月之暗面"` 同。
  - `test_reset_phrases`：三句 reset 都 → `kind == "reset"`。
  - `test_question_mentioning_persona_is_not_command`：`"毒舌是什么意思"`、`"换成毒舌模式好吗？"` → `None`。
  - `test_unknown_name_is_not_command`：`"换成海盗模式"` → `None`。
- [ ] **Step 2:** `python -m unittest tests.hub.test_commands -v` → FAIL。
- [ ] **Step 3:** 实现。
- [ ] **Step 4:** PASS。

### Task 4: OpenAI 兼容流式模型客户端

**Files:**
- Create: `hub/llm.py`
- Test: `tests/hub/test_llm.py`

**Interfaces:**
- Produces：`class ChatClient: __init__(self, base_url: str, api_key: str, model: str, temperature: float | None = None, max_tokens: int | None = None, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 45.0)`；
  `async def chat_stream(self, message: str, system_prompt: str | None = None, conversation_history: list | None = None) -> AsyncIterator[str]`（签名与 `server/streaming_pipeline.StreamingPipeline.process` 调用的 `llm.chat_stream(...)` 一致；`conversation_history` 可以已包含 system 之外的任意消息，联网资料也通过它传入）。
- 请求 `POST {base_url}/chat/completions`（`base_url` 末尾 `/` 去掉；若不以 `/v1` 结尾也直接拼，DeepSeek 两种都接受），`stream: true`，解析 SSE `data:` 行，产出 `choices[0].delta.content` 非空片段；HTTP 非 2xx 抛 `LLMError(status, body前200字)`。

- [ ] **Step 1: 写失败测试**（`httpx.MockTransport` 返回 SSE 文本）：
  - `test_streams_content_and_sends_messages`：两个 delta `"你好"`、`"。"` + `data: [DONE]` → 收集结果 `"你好。"`；断言请求体 `messages[0] == {"role":"system","content":"S"}`，最后一条为 user 消息，`model` 与 `Authorization: Bearer k` 正确。
  - `test_http_error_raises`：返回 401 → `LLMError`。
- [ ] **Step 2:** `python -m unittest tests.hub.test_llm -v` → FAIL。
- [ ] **Step 3:** 实现（`httpx.AsyncClient.stream`）。
- [ ] **Step 4:** PASS。

### Task 5: 联网搜索模块

**Files:**
- Create: `hub/web.py`
- Test: `tests/hub/test_web.py`

**Interfaces:**
- Consumes：DeepSeek 供应商 dict（Task 1 的 `search_provider_id` 指向的记录）。
- Produces：
  - `@dataclass class WebResult: action: str; evidence_messages: list[dict]; sources: list[str]`，`action ∈ {"off","answer","search","fetch"}`。
  - `async def ground(question: str, history: list[dict], provider: dict, transport: httpx.AsyncBaseTransport | None = None, now: datetime | None = None) -> WebResult`。
- 行为：移植 `device/native_first_client.sh` 中 `# BEGIN GENERATED NATIVE WEB` 至 `# END GENERATED NATIVE WEB` 段的规划提示词与流程：先用规划调用判断 `answer/search/fetch`（规划超时 4 s）；`search` 调 `/v1/tools/search`（`{"query":...,"count":3,"need_content":false}`）、`fetch` 调 `/v1/tools/fetch`（`url` 只能取自历史中已出现的 URL），工具超时 8 s，总预算 12 s；成功时 `evidence_messages` 为两条：system（照抄设备端"当前时间：…最后一个用户问题已完成联网检索…"那段文字，时间用 `now`）+ user（`外部参考资料：\n<evidence>`）；任何失败返回 `action="off"`、空列表（回答照常进行，不报错）。
- 实现前先通读设备端那一段，复制其提示词原文，不要改写含义。

- [ ] **Step 1: 写失败测试**（MockTransport 按 URL 路径返回）：
  - `test_plan_answer_needs_no_tools`：规划返回 `{"action":"answer"}` → `action == "answer"`、`evidence_messages == []`。
  - `test_plan_search_builds_evidence`：规划返回 search + query，search 返回 1 条结果（title/url/published/summary）→ `action == "search"`，`len(evidence_messages) == 2`，第二条 content 以 `外部参考资料：` 开头且含该 title，`sources == [url]`。
  - `test_fetch_rejects_url_not_in_history`：规划要求 fetch 一个历史中没有的 URL → `action == "off"`，且未请求 `/tools/fetch`。
  - `test_tool_failure_degrades_to_off`：search 返回 500 → `action == "off"`。
- [ ] **Step 2:** `python -m unittest tests.hub.test_web -v` → FAIL。
- [ ] **Step 3:** 实现。
- [ ] **Step 4:** PASS。

### Task 6: 对话服务与设备接口

**Files:**
- Create: `hub/chat.py`、`hub/app.py`
- Test: `tests/hub/test_app.py`

**Interfaces:**
- Consumes：Task 1–5 全部接口；`server.streaming_pipeline.StreamingPipeline(llm_client, tts_client, sample_rate=32000, speed, volume)` 的 `process(message, system_prompt, conversation_history)`（产出 `{"type":"text"|"audio"|"done"|"error"}`）与 `_split_sync`、`_tts_stream`；`server.tts.edge_tts_client.EdgeTTSClient(voice)`。
- Produces：
  - `hub/chat.py`：`class ChatService: __init__(self, store: Store, llm_factory=ChatClient, tts_factory=EdgeTTSClient, web_ground=ground, clock=time.time)`；`async def answer(self, text: str, speed: float, volume: float) -> AsyncIterator[bytes]`（只产出 PCM，不含 WAV 头；首个 PCM 产出前的任何失败直接抛出）；`async def speak(self, text: str, speed: float, volume: float) -> AsyncIterator[bytes]`（纯 TTS，当前性格音色）。
  - `hub/app.py`：`create_app(store: Store, service: ChatService | None = None) -> FastAPI`；模块级 `app = create_app(Store(os.environ.get("HUB_DB", <项目根>/hub.db)))` 仅在 `__main__`/uvicorn 入口创建（测试不触发）。路由：`GET /` → `{"status":"ok"}`；`POST /api/v1/stream/text_chat`；`POST /api/v1/tts/stream`。
- `answer` 流程：`parse_command` 命中 → 改设置（性格/供应商写 `current_*`；reset 记 `kind="reset"`）、`add_turn(kind="command"/"reset")`、TTS 确认语（性格：`好的，已切换为{name}`；模型：`好的，已切换到{name}`；reset：`好的，我们重新开始`），确认语用切换**后**的音色；未命中 → `select_context(store.recent_turns(max_turns*2+5), clock(), gap, max_turns)` → `ground(...)`（`search/fetch` 时先 TTS 播 `搜索中，请稍等。`）→ system prompt = `base_prompt` + `\n` + 当前性格 prompt → `StreamingPipeline(ChatClient(当前供应商), EdgeTTSClient(当前性格 voice)).process(text, system_prompt, context + evidence_messages)`；累积 text；正常结束 `add_turn(kind="qa")`；生成器被关闭（`GeneratorExit`/`asyncio.CancelledError`，即设备断开）时在 `finally` 里 `add_turn(..., interrupted=True)` 保存已累积文本，并确保底层模型流被关闭。
- 路由层：沿用 `server/main.py` `stream_tts` 的做法——先 `await gen.__anext__()` 预取首块，失败返回 HTTP 502；成功后返回 `StreamingResponse`（RIFF 头 + 首块 + 其余），`headers={"X-Accel-Buffering":"no"}`。

- [ ] **Step 1: 写失败测试**（`fastapi.testclient.TestClient`；假 LLM 工厂产出固定 token，假 TTS 工厂对每句产出 `b"\x01\x00"*160`，假 `web_ground` 默认返回 `WebResult("answer",[],[])`；`clock` 可控）：
  - `test_health`：`GET /` → 200 `{"status":"ok"}`。
  - `test_text_chat_streams_wav_and_saves_turn`：POST form `message=世界首富是谁` → 200，`content[:4] == b"RIFF"`，`len(content) > 44`；`store.recent_turns(1)[0]` 的 `kind=="qa"`、`answer` 为假 LLM 全文、`persona_name=="默认"`。
  - `test_uses_persona_prompt_and_voice`：当前性格设为毒舌 → 假 LLM 收到的 `system_prompt` 含毒舌 prompt，假 TTS 工厂收到 voice `zh-CN-YunxiNeural`。
  - `test_command_switches_without_llm`：`message=换成温柔模式` → 假 LLM 未被调用；`current_persona()["name"]=="温柔"`；假 TTS 收到文本 `好的，已切换为温柔` 且 voice 为温柔的音色。
  - `test_context_passed_within_gap`：先问 A（clock=1000），再问 B（clock=1100）→ 第二次假 LLM 收到的 `conversation_history` 含 A 的问答；clock=2000 再问 C → 不含。
  - `test_search_plays_filler_first`：假 `web_ground` 返回 `action="search"` 和两条 evidence → 假 TTS 第一次收到 `搜索中，请稍等。`；假 LLM 收到的 history 末尾两条为 evidence。
  - `test_model_error_before_audio_returns_502`：假 LLM 第一次迭代就抛异常 → 502，且不新增历史。
  - `test_disconnect_saves_partial_interrupted`：直接驱动 `service.answer(...)`：取到第一块 PCM 后 `await gen.aclose()` → `recent_turns(1)[0]["interrupted"] == 1`，`answer` 非空且是全文前缀。
  - `test_tts_stream_endpoint`：`POST /api/v1/tts/stream message=你好` → 200 RIFF，假 TTS voice 为当前性格音色。
- [ ] **Step 2:** `python -m unittest tests.hub.test_app -v` → FAIL。
- [ ] **Step 3:** 实现 `hub/chat.py`、`hub/app.py`。
- [ ] **Step 4:** PASS；再跑 `python -m unittest discover -s tests/hub -v` 全绿。

### Task 7: 设备退回配置接口

**Files:**
- Modify: `hub/app.py`
- Test: `tests/hub/test_fallback.py`

**Interfaces:**
- Produces：`GET /api/v1/device/fallback`，请求头 `X-Device-Token` 必须等于设置 `device_token`，否则 403 且响应体不含 key；成功返回 JSON `{"base_url":..., "api_key":..., "model":..., "system_prompt": base_prompt + "\n" + 当前性格 prompt}`，供应商取 `fallback_provider_id`。

- [ ] **Step 1: 写失败测试**：
  - `test_requires_token`：无头 / 错误口令 → 403，响应文本不含种子 key（测试前 `upsert_provider` 把 DeepSeek key 设为 `sk-secret-1234`）。
  - `test_returns_fallback_provider`：正确口令 → 200，`api_key=="sk-secret-1234"`，`model=="deepseek-flash"`，`system_prompt` 含当前性格 prompt。
  - `test_fallback_independent_of_current`：新增 Kimi 并设为当前，`fallback_provider_id` 仍为 DeepSeek → 返回 DeepSeek。
- [ ] **Step 2:** `python -m unittest tests.hub.test_fallback -v` → FAIL。
- [ ] **Step 3:** 实现。
- [ ] **Step 4:** PASS。

### Task 8: 网页后台

**Files:**
- Create: `hub/admin.py`（`APIRouter`，服务端渲染 HTML 字符串，无前端构建）；Modify: `hub/app.py`（挂载 `/admin`）
- Test: `tests/hub/test_admin.py`

**Interfaces:**
- Produces：`make_admin_router(store: Store) -> APIRouter`。页面：`GET /admin`（总览：当前模型、当前性格、退回模型、设备口令、上下文参数，可下拉修改并提交）、`GET/POST /admin/providers`（列表 + 新增/编辑/删除表单）、`GET/POST /admin/personas`（含音色下拉：`server/tts/edge_tts_client.VOICES` 的值，另允许手填）、`GET /admin/history?page=N`（每页 50 条，倒序，显示时间、问题、回答、模型、性格、是否打断、是否联网）。表单 POST 后 303 重定向回列表页。
- 规则：API key 展示为 `****` + 后 4 位（空 key 显示 `未设置`）；编辑供应商时 key 输入框留空表示不修改；所有输出做 HTML 转义。

- [ ] **Step 1: 写失败测试**：
  - `test_admin_masks_key`：key 为 `sk-secret-1234` → `GET /admin/providers` 含 `****1234`，不含 `sk-secret`。
  - `test_edit_provider_blank_key_keeps_old`：POST 编辑表单 key 为空 → 库中 key 仍为原值。
  - `test_switch_current_persona_via_form`：POST `/admin` 设 `current_persona_id` → 303，`current_persona()` 已变。
  - `test_history_page_escapes_html`：历史中 user_text 为 `<script>` → 页面含 `&lt;script&gt;`。
  - `test_context_settings_validated`：提交 `context_gap_seconds=abc` → 不写入，页面显示错误。
- [ ] **Step 2:** `python -m unittest tests.hub.test_admin -v` → FAIL。
- [ ] **Step 3:** 实现。
- [ ] **Step 4:** PASS。

### Task 9: 启动入口与部署文件

**Files:**
- Create: `hub/__main__.py`、`hub/README.md`
- Modify: `docker-compose.yml`（新增 `hub` 服务，端口 `9999:9999`，挂载项目目录下 `hub.db` 所在路径为卷，`HUB_DB` 指向卷内文件）、`.gitignore`（加 `hub.db`）

**Interfaces:**
- `python -m hub` 启动 uvicorn `hub.app:app`，`host 0.0.0.0`、`port int(os.environ.get("HUB_PORT", 9999))`，不开 reload。启动前检查 `ffmpeg` 在 PATH（缺失时打印与 `EdgeTTSClient` 同义的错误并退出 1）。

- [ ] **Step 1:** 实现 `hub/__main__.py`；`hub/README.md` 写明：本地启动命令、`HUB_DB`/`HUB_PORT`、Windows 上 ffmpeg 的 `imageio-ffmpeg` 放法（引用 `recovery.md` 中已有说明）、NAS 端口转发 `192.168.100.100:9999 → 开发机:9999`、后台地址 `/admin`、设备口令在后台查看。
- [ ] **Step 2:** 本地冒烟：`python -m hub` 启动后，`curl -s http://127.0.0.1:9999/` → `{"status":"ok"}`；`curl -s -o /tmp/a.wav -F message=你好 http://127.0.0.1:9999/api/v1/tts/stream` 得到 RIFF 文件（需联网 EdgeTTS，属手工验证）。
- [ ] **Step 3:** `docker compose config` 校验通过。

### Hub 验收门槛（设备工作开始前必须完成）

- [ ] `python -m unittest discover -s tests/hub -v` 全部 PASS。
- [ ] 开发机 `python -m hub` 启动（真实 DeepSeek key、真实 EdgeTTS），用 curl 模拟设备逐项验证并记录：普通问答返回可播放 WAV；"世界首富是谁"触发联网且回答含来源和日期；"换成毒舌模式"后音色与语气变化、后台同步、重启 hub 后保持；"切换到 Kimi"（后台配了 Kimi 时）生效；5 分钟内追问带上下文、超过 5 分钟或"忘掉刚才的对话"后不带；curl 中途 Ctrl-C 后历史记录为"被打断"；`/api/v1/device/fallback` 无口令 403、有口令返回配置；后台页面 key 只显示后 4 位。
- [ ] 从 NAS 端口转发地址 `http://192.168.100.100:9999/` 访问正常。
- [ ] 把以上结果汇报给用户，**等用户确认后**再开始 Task 10。

### Task 10: 设备客户端接入 hub 与自动退回

**Files:**
- Modify: `device/native_first_client.sh`、`device/native_first.env.example`
- Test: `tests/test_native_hub_fallback.py`（沿用 `tests/test_native_client_failure_paths.py` 的 `functions(...)`/`shell(...)` 抽函数做法和本地 `http.server` 假服务）

**Interfaces:**
- 新 env（写入 `native_first.env.example` 并注释）：`HUB_DEVICE_TOKEN=`（空则不拉取）、`HUB_HEALTH_TIMEOUT=1`、`LED_YELLOW=65535`（LED 颜色为 BGR 整数，现有 `LED_ORANGE=33023`）。
- 新函数：
  - `hub_fetch_fallback_config()`：仅当 `LLM_PIPELINE=server` 且 `HUB_DEVICE_TOKEN` 非空时，`curl -s -m 3 -H "X-Device-Token: $HUB_DEVICE_TOKEN" "$SERVER/api/v1/device/fallback"`，用 `jsonfilter` 取 `base_url/api_key/model/system_prompt`，四个都非空才覆盖内存中的 `LLM_API_BASE`、`LLM_API_KEY`、`LLM_MODEL`、`LLM_SYSTEM_PROMPT`（不写盘）；失败记日志并保留原值。客户端启动阶段（`start_aivs_speech_guard` 调用附近）调用一次。
  - `hub_online()`：`curl -s -m "$HUB_HEALTH_TIMEOUT" -o /dev/null -w '%{http_code}' "$SERVER/"` 为 `200` 返回 0。
  - `led_feedback_fallback()`：黄灯常亮（参照 `led_feedback_error` 写法，颜色 `$LED_YELLOW`）。
- 修改 `send_text_and_play`：`LLM_PIPELINE=server` 时先 `hub_online`；不在线 → 记 `[HUB] offline, fallback to direct LLM`、`led_feedback_fallback`，本次调用 `send_text_native`（`TTS_SERVER` 同在 hub，`tts_play_text` 会因 `tts_service_online` 失败走原生 TTS——现有行为，不改）。在线 → 现有 server 路径，并补齐打断：`http_stream_play` 前 `rm -f "$LLM_BARGE_MARKER"; [ "$LLM_BARGE_IN" != 1 ] || echo "$$" > "$LLM_PLAYING_MARKER"`，之后删除 `$LLM_PLAYING_MARKER`；失败且 `$LLM_BARGE_MARKER` 存在 → 记"播报被新唤醒打断"并 `finish_llm_playback quiet`，不报错。
- 注意：`llm_web_init` 只在 `LLM_PIPELINE=native` 时启用联网（`native_first_client.sh` 约 2848 行）；退回时为了能联网，判断条件改为"本次实际走 native 路径"，实现者用一个本轮变量（如 `LLM_TURN_PIPELINE`）区分配置值与本轮实际值。

- [ ] **Step 1: 写失败测试**：
  - `test_fallback_fetch_overrides_in_memory`：假服务返回合法 JSON → 执行 `hub_fetch_fallback_config; echo "$LLM_MODEL|$LLM_API_KEY"` 输出假服务给的值；env 文件内容未变。
  - `test_fallback_fetch_failure_keeps_env`：假服务返回 403 / 非 JSON / 端口不通三种 → 变量保持原值，返回码 0。
  - `test_hub_online`：假服务 `/` 返回 200 → 0；关掉服务 → 非 0，且耗时 < `HUB_HEALTH_TIMEOUT + 1` 秒。
  - `test_server_path_sets_playing_marker`：桩 `http_stream_play` 在执行时检查 `$LLM_PLAYING_MARKER` 存在并写入其内容，结束后标记已删除。
  - `test_barge_in_on_server_path_is_quiet`：桩 `http_stream_play` 创建 `$LLM_BARGE_MARKER` 并返回 1 → `finish_llm_playback` 收到参数 `quiet`，`led_feedback_error` 未被调用。
  - `test_offline_routes_to_native`：`hub_online` 桩返回 1 → `send_text_native` 被调用，`led_feedback_fallback` 被调用。
- [ ] **Step 2:** `python -m unittest tests.test_native_hub_fallback -v`（WSL 内运行，与其他 device 测试一致）→ FAIL。
- [ ] **Step 3:** 实现；`sh -n device/native_first_client.sh` 通过。
- [ ] **Step 4:** 新测试 PASS；回归 `python -m unittest tests.test_native_client_failure_paths tests.test_aivs_speech_guard -v` 仍 PASS。

### Task 11: 实机切换与验收（手工，需用户配合断电）

**Files:**
- Modify: `recovery.md`（记录切换结果）

- [ ] **Step 1:** 开发机启动 `python -m hub`；在后台填 DeepSeek key、核对 4 个性格音色；记下设备口令。确认 NAS 已配端口转发 `192.168.100.100:9999 → 192.168.100.99:9999`，在开发机外的设备上 `curl http://192.168.100.100:9999/` 返回 ok。
- [ ] **Step 2:** 用调试代理进设备（流程见 `recovery.md`"排障技巧"），先读一遍 `/data/native_first.env` 全文，核对 `TTS_ENGINE=server`、追问相关项关闭；如与 spec 假设不符，停下来报告。
- [ ] **Step 3:** 部署新 `native_first_client.sh`；备份 env 为 `native_first.env.pre_hub` 后改：`SERVER=http://192.168.100.100:9999`、`TTS_SERVER=http://192.168.100.100:9999`、`LLM_PIPELINE=server`、`HUB_DEVICE_TOKEN=<口令>`；重启，确认客户端日志出现退回配置拉取成功。
- [ ] **Step 4:** 逐条执行 spec §9 验收清单，每条记录结果；包括关掉 hub 后问答走小爱原声 + 黄灯、恢复后回到 hub。
- [ ] **Step 5:** 移除调试代理、恢复 `init.sh`；`recovery.md` 追加记录。
