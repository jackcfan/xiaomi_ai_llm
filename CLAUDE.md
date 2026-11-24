# CLAUDE.md

## 强制约束（违反即错）

- **禁止**自动执行 `git add` / `git commit` / `git push`——用户明确说”帮我提交”才执行

## 项目定位

这是一个跑在**小米 AI 音箱 MDZ-25-DA（系统代号 S12A）**上的「native-first（原生优先）」客户端：小爱能处理的开灯/天气/报时仍走小米原生链路，答不上来的开放问题由音箱**直连**云端 LLM（DeepSeek / GLM / Kimi / MiniMax），再用 EdgeTTS 合成播报，播完进入免唤醒追问。模型不在音箱上运行（非离线方案）。本仓库自有代码 MIT 许可，非小米官方产品。

开发机是 **Windows**（PowerShell 为主），但设备端跑 **BusyBox ash**，仓库里的 shell 脚本必须兼容 `sh`（用 `sh -n` 校验），不能写 bash 专有语法。仓库内同时可用 Bash 工具（Git Bash，POSIX sh）跑 POSIX 脚本。

## 常用命令

### 测试（unittest，仓库没有 pytest）

```sh
# 完整回归：纯协议嵌入一致性检查 + unittest + Shell 语法检查（sh -n）
sh scripts/run_tests.sh            # 也可 PYTHON=.venv/Scripts/python.exe sh scripts/run_tests.sh

# 单个测试（模块路径形式）
python -m unittest tests.test_native_web -v
python -m unittest tests.hub.test_store -v
python -m unittest discover -s tests/hub -v
```

- 测试用 `unittest`，不用 pytest。设备端测试（如 `tests/test_native_client_failure_paths.py`）用 `subprocess.Popen(['sh', '-c', ...])` 抽出 `device/native_first_client.sh` 里的函数在隔离 shell 里跑，依赖 POSIX sh。
- `tests/hub/` 的测试**不得访问真实网络**：用 `httpx.MockTransport` 或假对象。新测试建库用 `tempfile`（SQLite 文件路径），不碰真实 MySQL。

### 嵌入产物一致性（改协议源码后必跑）

设备主客户端 `device/native_first_client.sh` 是单文件部署产物，其中三段由独立源文件生成、夹在 `# BEGIN GENERATED ...` / `# END GENERATED ...` 标记之间：

| 生成段 | 源文件 | 生成脚本 |
|---|---|---|
| NATIVE PROTOCOL | `device/lib/native_protocol.sh` | `scripts/embed_native_protocol.py` |
| NATIVE WEB | `device/lib/native_web.sh` | `scripts/embed_native_web.py` |
| ROUTE BEEP | 生成自 `scripts/generate_dongdong.py` | `scripts/embed_route_beep.py` |

**改协议/联网逻辑时改 `device/lib/` 里的源文件，再运行对应 embed 脚本重新生成；不要直接改客户端里的生成段**（`--check` 会失败）。`run_tests.sh` 会对三者跑 `--check`。

### 启动 hub

```sh
# NAS LLM hub（端口 9999，MySQL 持久化 + /admin 网页后台），必须用 -m 入口
.venv/Scripts/python.exe -m hub           # Windows；Linux 用 .venv/bin/python -m hub
sh start.sh                               # 等价封装（POSIX，nohup 后台起 hub，日志进 hub.log）
# 环境变量：HUB_PORT（默认 9999）、DEEPSEEK_API_KEY（仅首次建表播种）、
# FN_MYSQL_PWD（必填，MySQL 密码，只从环境变量读取，绝不写进工程）
```

hub 依赖 ffmpeg（EdgeTTS MP3→PCM）。Windows 未装系统 ffmpeg 时用 `imageio-ffmpeg` 复制到 `.venv/Scripts/ffmpeg.exe`（见 `hub/README.md`）。

`server/` 下的 FastAPI 服务（`sh start_server.sh` / `python -m server.run`）当前版本已废弃，改用 hub；`server/` 目录仅保留 hub 复用的库代码（`streaming_pipeline.py`、`tts/edge_tts_client.py`）。

### 依赖与虚拟环境

- 无 pyproject/setup：依赖写在 `requirements.txt`（完整：含 Whisper ASR、LLM SDK）与 `requirements-tts.txt`（轻量：仅 TTS + hub 用）。
- 脚本里 `.venv/bin/python`（POSIX 布局）；Windows 虚拟环境是 `.venv/Scripts/python.exe`。本机 Python 3.12。

### 访问 NAS 数据库（dbx MCP）

本地 NAS `fnos.itougu.tech:3306` 跑 MySQL，dbx MCP 已配好连接名 `nas`（无默认库），业务库 schema 为 `xiaomi_hub`。

- 查连接/库/表：`dbx_list_connections` / `dbx_list_databases` / `dbx_list_tables`（传 `connection_name="nas"`）。
- 跑 SQL：先 `dbx_open_session(connection_name="nas", database="xiaomi_hub")` 拿 `session_id`，再 `dbx_execute_query(session_id=..., connection_name="nas", database="xiaomi_hub", sql=...)`，用完 `dbx_close_session(session_id=...)`。
- 注意：`dbx_execute_query` 必须显式传 `connection_name`（否则报 `CONNECTION_NOT_FOUND`）；带 `session_id` 时 `database` 也要显式传且与 session 绑定的库一致（否则报 `SESSION_DATABASE_MISMATCH`）。

## 架构

### 运行位置

| 环节 | 在哪运行 |
|---|---|
| 唤醒、收音、路由、音频播放、LED 灯效 | 音箱本机（`device/native_first_client.sh` 主状态机） |
| 语音识别、家电控制、小爱原生回答 | 小米云 |
| 大模型 + EdgeTTS | 你配置的云 API（音箱直连）或 hub |

音箱直连 LLM 主线（`LLM_PIPELINE=native`）不依赖常驻电脑；`TTS_ENGINE=device` 时连 TTS 都在音箱上（`ettsc` 组件）。开发机只用于部署维护。

### 双系统（关键约束）

同一台设备有两套原生系统 boot0 / boot1，用户态不同，客户端需分别适配结果源与路由方式：

- **boot0**（ROM 1.54.8，system0）：读 `mibrain nlp_result_get` 的 `domain/action/query/to_speak`，结构化字段 + 文本辅助路由。
- **boot1**（ROM 1.76.54，system1）：读 AIVS 日志的非空 `final` 与 `Speak.text`，先匹配触发词（DeepSeek 大小写），否则匹配失败文案才接管；有原生实时 ASR-only 追问组件。

不要把一套系统的二进制/库「补齐」另一套。双系统能力对照见 `docs/status.md`。

### 三个顶层组件

1. **`device/`** — 音箱主客户端（单文件 `native_first_client.sh`，~700KB，含生成段）+ 各 C 组件：`aivs_guard`（失败播报拦截）、`native_asr`（boot1 原生追问）、`native_endpoint`（boot1 首轮本地 VAD 判停）、`ettsc`（端侧 EdgeTTS，Rust）、`pcm_tap` / `micnorm`（旧录音路线）等。每个子目录有独立 `README.md`（构建/ABI/安装基线）。设备配置：`native_first.env.example` → 复制到音箱 `/data/native_first.env`。
2. **`server/`** — 已废弃的 FastAPI 服务（`server/main.py`，原 `full`/`tts-only` 模式）。目录本身保留，因为 hub 复用其中的 `streaming_pipeline.py`（LLM 流式输出→实时分句→逐句 TTS→边生成边下发 PCM）与 `tts/edge_tts_client.py`。
3. **`hub/`** — NAS LLM hub（`python -m hub`，端口 9999）：当前的服务端实现，独立的 FastAPI 服务。设备把文字发来，hub 调 LLM + 联网搜索 + EdgeTTS，返回流式 WAV（32kHz/16bit/单声道）。所有可变配置存 NAS MySQL（`xiaomi_hub` 库），在 `/admin` 网页后台管理。对设备暴露 `POST /api/v1/stream/text_chat`、`/api/v1/tts/stream`、`GET /api/v1/device/fallback`（设备口令 `X-Device-Token` 保护）。设备端在 hub 不在线时自动退回直连 DeepSeek。

### 配置分层

- **设备端**：`device/native_first.env.example` 全量注释说明；设备直连时 API key 直接写在 `/data/native_first.env`（不进仓库）。
- **hub**：只认 MySQL（`xiaomi_hub` 库），不读 `config.yaml`；模型、人设、音色、API key 等都在 `/admin` 后台管理。

### 文档结构

`docs/` 是主要知识库，按「导读 README → getting-started（安装）→ concepts（机制：native-first / boot-and-partitions / comparison / glossary）→ runbooks（日常操作/排障/自启动/双系统维护）→ reference（硬件/配置/诊断/服务端/协议/联网搜索）→ history（探索证据）」组织。修改行为后要同步 `docs/status.md`（当前能力与验收边界），并交付一份 `docs/testing/YYYY-MM-DD-*.md` 实际测试说明。

## 约定与注意事项

- 修改设备端 shell 后跑 `sh -n`（`run_tests.sh` 已覆盖主要脚本）；保持 BusyBox ash 兼容。
- hub 的 API key 只在后台显示后 4 位，任何日志不得打印 key；MySQL 里的 API key 只存 `xiaomi_hub` 库，`FN_MYSQL_PWD`（MySQL 密码）只从环境变量读取，勿提交、勿外传。
- 设备端测试不要访问真实网络、不要把文件回放描述为真人现场验收（见 `TESTING.md` 的分层验证原则）。
- 提交前按 `TESTING.md` §4 选择对应检查；仅文档改动需跑 `git diff --check` 并核对相对链接/锚点。
