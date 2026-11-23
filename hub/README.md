# NAS LLM hub

独立的 FastAPI 服务：音箱把识别出的文字发给 hub，hub 调 LLM（默认 DeepSeek，可在后台切换）、按需联网搜索，
再合成语音并以流式 WAV（32 kHz / 16-bit / 单声道，与 `server/main.py` 同格式）返回。
语音合成默认用 EdgeTTS（免费、微软），每个**性格**可选硅基流动的 CosyVoice2（`FunAudioLLM/CosyVoice2-0.5B`）。
模型、人设、音色、历史记录都存在 NAS MySQL（`xiaomi_hub` 库），在网页后台 `/admin` 管理。

## 语音合成（TTS 提供方）

性格（persona）的「服务提供方」决定用哪家合成：

- **EdgeTTS（默认）**：免费、无需 key。`personas.voice` 填微软音色名（如 `zh-CN-YunjianNeural`）。
- **硅基流动 CosyVoice2**：`personas.tts_provider = siliconflow`，`personas.voice` 填 CosyVoice2 的
  voice id（如 `speech:sqz:...`）。**API key 存 `providers` 表**：在 `/admin/providers` 新增一条
  「硅基流动」供应商（名称或别名含 `siliconflow`），key 填进它的 API key 字段即可；`base_url` 默认
  `https://api.siliconflow.cn/v1`。模型固定为 `FunAudioLLM/CosyVoice2-0.5B`（不用 provider 的 model）。

CosyVoice2 请求 `response_format=wav` + `sample_rate=32000`，拿到后走与 EdgeTTS 相同的 ffmpeg 响度
归一化（`dynaudnorm`+`loudnorm`+`alimiter`），保证不同引擎在音箱上响度一致。选硅基流动但 providers
里没有对应供应商时，回答在首块音频前失败（HTTP 502），日志提示去 `/admin/providers` 补 key。

## 本地启动（Windows 开发机）

```powershell
$env:FN_MYSQL_PWD = "<xiaomi_hub_user 的 MySQL 密码>"   # 只从环境变量读取，绝不写进工程
.venv/Scripts/python.exe -m hub
```

- 监听 `0.0.0.0:9999`，不开 reload。请始终用 `python -m hub` 启动（不要 `python -m hub.app`），否则会跳过
  下面的 ffmpeg 检查与 UTF-8 输出修正。
- 启动前检查 `ffmpeg`：PATH 上找不到时，会再看解释器所在目录（即 `.venv/Scripts`），找到就自动加进 PATH；
  都没有则打印 `EdgeTTS requires ffmpeg on PATH ...` 并以退出码 1 结束。
- stdout/stderr 被强制改为 UTF-8（`errors="replace"`），避免日志重定向到文件时逐句打印的中文/emoji 触发
  `UnicodeEncodeError` 打断 TTS。

### 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FN_MYSQL_PWD` | 无（**必填**） | MySQL 密码，**只从环境变量读取**，绝不写进代码/仓库/镜像。未设置时启动报错退出 |
| `HUB_DB_HOST` | `fnos.itougu.tech` | MySQL 主机 |
| `HUB_DB_PORT` | `3306` | MySQL 端口 |
| `HUB_DB_NAME` | `xiaomi_hub` | MySQL 库名 |
| `HUB_DB_USER` | `xiaomi_hub_user` | MySQL 用户 |
| `HUB_PORT` | `9999` | 监听端口 |
| `DEEPSEEK_API_KEY` | 空 | 仅在**首次建表播种**时写入默认 DeepSeek 服务商；之后以后台配置为准 |

启动时自动建表（`CREATE TABLE IF NOT EXISTS`），`settings` 表为空时播种默认服务商与四个默认人设。

> **登录被拒（1045）排查**：报 `Access denied for user 'xiaomi_hub_user'@'<IP>'` 时，先确认 `FN_MYSQL_PWD`
> 与 MySQL 里 `xiaomi_hub_user` 的实际密码一致；若密码正确仍被拒，是 `<IP>`（容器场景通常是
> `172.17.0.1` 这类 Docker 网桥地址，或 NAS 本机 `172.17.0.1`）不在允许来源内 —— 服务端已明确
> **未设置 IP 白名单**，请核对 `mysql.user` 里 `xiaomi_hub_user` 的 `host`（当前为 `%`，即任意来源）。

### ffmpeg（Windows）

EdgeTTS 输出 MP3，需要 ffmpeg 转成 PCM（`EdgeTTSClient` 构造时找不到 ffmpeg 会直接报错，整句 TTS 失败）。
没装系统版 ffmpeg 时，用 `imageio-ffmpeg` 自带的二进制，复制到 `.venv/Scripts/ffmpeg.exe`；
`python -m hub` 启动时会自动把解释器所在目录加进 PATH，无需改系统环境变量。装好后用
`.venv/Scripts/ffmpeg.exe -version` 确认可运行。

```powershell
.venv/Scripts/python.exe -m pip install imageio-ffmpeg
$src = .venv/Scripts/python.exe -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
Copy-Item $src .venv/Scripts/ffmpeg.exe
```

### 冒烟

```bash
curl -s http://127.0.0.1:9999/                       # {"status":"ok"}
printf '你好' > msg.txt                               # 用文件传参，避免 Windows curl 把中文按 GBK 发送
curl -s -o a.wav --form "message=<msg.txt" http://127.0.0.1:9999/api/v1/tts/stream   # RIFF 开头的 WAV
```

## NAS 部署与端口转发

- 方式一（开发机跑 hub）：在 NAS 上把 `192.168.100.100:9999` 转发到 `开发机:9999`，音箱和浏览器都访问
  `http://192.168.100.100:9999/`。
- 方式二（NAS 上跑容器）：`docker compose up -d hub`。镜像与 `xiaomi-tts` 共用（已含 ffmpeg、`server/`、`hub/`），
  数据存在 NAS MySQL（`xiaomi_hub` 库），不再挂 SQLite 卷。启动前在宿主机设置 `FN_MYSQL_PWD`
  （compose 里以 `${FN_MYSQL_PWD:?...}` 强制要求，未设置会拒绝启动）。首次建表可先 `export DEEPSEEK_API_KEY=...`
  播种默认服务商，也可以留空、启动后在后台填写。

## 后台与设备口令

- 后台：`http://<地址>:9999/admin`（设置、服务商 `/admin/providers`、人设 `/admin/personas`、历史 `/admin/history`）。
- 设备口令（`device_token`）首次建表时随机生成，在 `/admin` 页面查看；设备请求兜底配置时放在 `X-Device-Token` 头里。
- 后台展示的 API key 只显示后 4 位。
- 服务商的“附加请求体 JSON”会合并进每次回答请求（不能覆盖 `model`/`messages`/`stream`），用于关闭隐藏思考等
  厂商参数，例如 DeepSeek / Kimi 填 `{"thinking":{"type":"disabled"}}`（与音箱直连一致，首句更快）。
  新建库时默认 DeepSeek 已预置该值；**本次升级前就已创建的表**会自动补上该列但值为空，
  请到 `/admin/providers` 为 DeepSeek（及其他需要的服务商）手动填上。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 健康检查，返回 `{"status":"ok"}` |
| POST | `/api/v1/stream/text_chat` | 表单 `message`（必填）、`speed`、`volume`；`session_id`、`backend` 为兼容字段被忽略。返回流式 WAV |
| POST | `/api/v1/tts/stream` | 表单 `message`、`speed`、`volume`；只做文本转语音，返回流式 WAV |
| GET | `/api/v1/device/fallback` | 头 `X-Device-Token`；口令不符 403，未配置服务商 503，否则返回 `base_url`/`api_key`/`model`/`system_prompt` |
| GET/POST | `/admin`、`/admin/providers`、`/admin/personas` | 网页后台 |
| GET | `/admin/history` | 对话历史 |

首块音频产出前失败返回 502；成功后才发送 200 与 WAV 头。

## 安全提示

- `/admin` **没有登录**，任何能访问端口的人都能改配置、看历史。只在局域网使用，不要把 9999 端口暴露到公网。
- 后台 POST 做同源检查：`Origin`（缺失时看 `Referer`）的 host:port 与请求 `Host` 不一致即返回 403，
  防止局域网浏览器里打开的其他网页跨站改配置；两个头都没有（curl/脚本）时放行。若在前面加了会改写 `Host`
  的反向代理，需要让代理保留原始 `Host`。
- `/api/v1/device/fallback` 通过明文 HTTP 返回**完整 API key**，仅靠设备口令保护；局域网内被抓包即泄露。
  怀疑泄露时在服务商处轮换 key，并在后台更换设备口令。
- MySQL 里的 API key 只存 `xiaomi_hub` 库；`FN_MYSQL_PWD`（MySQL 密码）只从环境变量读取，不要写进工程、仓库或镜像。
