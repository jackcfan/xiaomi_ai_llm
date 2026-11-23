# hub 语音合成扩展：硅基流动 CosyVoice2

日期：2026-11-23

## 改了什么

hub 的语音合成原本只有 EdgeTTS。本次为**性格（persona）**增加「TTS 服务提供方」，新增
硅基流动 CosyVoice2（`FunAudioLLM/CosyVoice2-0.5B`）：

- `personas` 表新增列 `tts_provider`（`''`/`edgetts` 默认 EdgeTTS，`siliconflow` 用 CosyVoice2）；
  旧库启动时自动补列、旧行默认 EdgeTTS，无需数据迁移。
- 声音源仍是 `personas.voice`：EdgeTTS 填音色名，CosyVoice2 填 voice id（如 `speech:sqz:...`）。
- **API key 存 `providers` 表**（按需求）：`/admin/providers` 新增一条「硅基流动」，名称或别名含
  `siliconflow`，key 填进它的 API key 字段，`base_url` 默认 `https://api.siliconflow.cn/v1`。
  模型固定为 `FunAudioLLM/CosyVoice2-0.5B`，不读 provider 的 `model`（那是 LLM 模型名）。
- 后台 `/admin/personas` 表单新增「服务提供方」下拉；音色沿用原「手填优先」输入。
- CosyVoice2 请求 `response_format=wav` + `sample_rate=32000`，拿到 WAV 后走与 EdgeTTS 相同的
  ffmpeg 响度归一化（`dynaudnorm`+`loudnorm`+`alimiter`），保证不同引擎在音箱上响度一致。
- 选硅基流动但 providers 里没有对应供应商时，回答在首块音频前失败（HTTP 502），日志提示去后台补 key；
  不会拿其它厂商的 key 发往硅基流动。

## 文件

- `server/tts/siliconflow_tts.py`：`SiliconFlowTTSClient`（新）。
- `hub/tts.py`：新增 `siliconflow_tts_factory`，与 EdgeTTS 一样包 `ThreadedTTS`（ffmpeg 阻塞进线程）。
- `hub/store.py`：`personas.tts_provider` 列 + 迁移 + `persona_tts_engine()` / `find_tts_provider()`。
- `hub/chat.py`：`_make_tts(persona)` 按提供方构造客户端。
- `hub/admin.py`：persona 表单加「服务提供方」下拉。
- `hub/README.md`：新增「语音合成（TTS 提供方）」一节。

## 验证（本机，未访问真实网络/真实硅基流动服务）

- `unittest`：`tests/test_siliconflow_tts.py`（`httpx.MockTransport` 断言请求载荷、无 key 泄漏；
  ffmpeg 归一化有 ffmpeg 时验证输出 32k/单声道/16bit）、`tests/hub/test_store.py`（列迁移 + 引擎回读 +
  按名/别名找 provider）、`tests/hub/test_app.py`（siliconflow 人设走 provider key / 缺失时 ChatError）、
  `tests/hub/test_admin.py`（表单保存与渲染）。
- `tests/hub` 全量 113 项通过（`PYTHONUTF8=1`）。全仓库 `unittest discover` 未完整跑通：
  Windows（无 `sh`/`os.O_NOFOLLOW`/ffmpeg 等 POSIX 依赖）下设备端 shell 抽测与 server 传输测试报错，
  与本次改动无关，属既有平台差异；相关新增测试单独跑通。

## 未验证 / 待用户

- 真实调用硅基流动 API（需要真实 `api_key`，本机不触发）；CosyVoice2 音色在音箱上的实际响度、听感。
- 生产 MySQL（`xiaomi_hub` 库）上的 `personas.tts_provider` 列迁移（自动 `ALTER TABLE`，逻辑与
  `providers.extra_body` 迁移一致，但未在真实库上执行过）。
