"""``python -m hub``：启动 hub（uvicorn ``hub.app:app``，0.0.0.0:HUB_PORT，默认 9999，不开 reload）。"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

FFMPEG_MISSING = (
    "EdgeTTS requires ffmpeg on PATH; install ffmpeg and verify `ffmpeg -version` before starting the hub"
)


def ensure_ffmpeg() -> bool:
    """ffmpeg 在 PATH 上返回 True；否则若解释器同目录（如 .venv/Scripts）有 ffmpeg，则把该目录加进 PATH。"""
    if shutil.which("ffmpeg"):
        return True
    scripts = str(Path(sys.executable).resolve().parent)
    if shutil.which("ffmpeg", path=scripts):
        os.environ["PATH"] = scripts + os.pathsep + os.environ.get("PATH", "")
        return True
    return False


def hub_port() -> int:
    return int(os.environ.get("HUB_PORT", "9999"))


def main() -> int:
    # server/streaming_pipeline.py 逐句打印 emoji/中文；Windows 下 stdout 重定向到非 UTF-8 文件会
    # 抛 UnicodeEncodeError 打断 TTS，这里统一改为 UTF-8 并替换无法编码的字符。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not ensure_ffmpeg():
        print(FFMPEG_MISSING, file=sys.stderr)
        return 1

    import uvicorn

    uvicorn.run("hub.app:app", host="0.0.0.0", port=hub_port(), reload=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
