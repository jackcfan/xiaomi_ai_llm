# 小米 AI 音箱服务端容器镜像
#
# 默认以 tts-only 模式启动：音箱自己直连 LLM（LLM_PIPELINE=native），
# 本服务只负责把文本合成为语音。full 模式还会加载 whisper ASR，
# 首次启动要下载约 1.4G 模型，不适用于 NAS 这类长期运行场景。
FROM python:3.12-slim

# EdgeTTS 客户端硬依赖 ffmpeg（server/tts/edge_tts_client.py 用 shutil.which 查找）
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 只安装 TTS 所需依赖：不装 whisper / anthropic / openai，镜像小很多
COPY requirements-tts.txt ./
RUN pip install --no-cache-dir -r requirements-tts.txt

COPY server ./server
COPY hub ./hub
COPY config.yaml ./

EXPOSE 8080
# hub 服务（docker-compose 里以 command: python -m hub 启动）
EXPOSE 9999

# --no-reload：容器里不需要热重载，reloader 子进程还会丢掉运行时 PATH
# --mode tts-only：跳过 ASR 与 LLM 初始化
CMD ["python", "-m", "server.run", "--no-reload", "--mode", "tts-only"]
