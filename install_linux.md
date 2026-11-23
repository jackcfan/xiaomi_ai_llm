1. 创建虚拟环境并安装依赖

python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements-tts.txt

2. 准备 ffmpeg（EdgeTTS 转 MP3→PCM 硬依赖）

Debian/Ubuntu 直接装系统版：

sudo apt-get install -y ffmpeg
ffmpeg -version   # 确认可运行

没有 apt（或不想装系统版）时，用 imageio-ffmpeg 自带的二进制复制到 venv 里：

.venv/bin/python -m pip install imageio-ffmpeg
src=$(.venv/bin/python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())")
cp "$src" .venv/bin/ffmpeg
.venv/bin/ffmpeg -version   # 确认可运行

3. 设置 MySQL 密码（必填，只从环境变量读）

export FN_MYSQL_PWD="<xiaomi_hub_user 的密码>"

可选的其他环境变量（都有默认值，一般不用设）：

export HUB_PORT="9999"              # 默认 9999
export DEEPSEEK_API_KEY="sk-..."    # 仅首次建表播种默认服务商用，可留空

4. 启动

.venv/bin/python -m hub

必须用 python -m hub（不要 python -m hub.app），否则会跳过 ffmpeg 检查和 UTF-8 输出修正。
后台常驻可用仓库自带的 start.sh（POSIX，nohup 起 hub，日志进 hub.log）：

sh start.sh

5. 验证

curl -s http://127.0.0.1:9999/     # 应返回 {"status":"ok"}

浏览器打开 http://127.0.0.1:9999/admin 看到后台即正常；首次启动会自动建表并播种默认 DeepSeek + 4 个人设。
