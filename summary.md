# 小米 AI 音箱接入全流程总结

> 生成时间：2026-11-22
> 最后更新：2026-11-22（system1 恢复 + 自启动 + LLM/TTS 全链路跑通）
> 设备：小米 AI 音箱 MDZ-25-DA / S12A（Broadcom BCM43455 批次）
> 目标：将音箱直连 DeepSeek，用端侧/服务端 TTS 播报 LLM 回答
> 当前状态：**已跑通**。system1 重启后可挂载、`/data/init.sh` 自启动生效、设备调用服务端 TTS 返回 200。

## 1. 背景与目标

老款小米 AI 音箱（MDZ-25-DA / S12A）拆机后，通过主板 JST 串口（免焊）接入 USB-TTL，打通 SSH 或临时 shell，将 `native_first_client.sh` 部署到 `/data`，实现：小爱答不上来的问题自动转给 DeepSeek，用 EdgeTTS 播报。

本项目（open-speaker-llm/xiaomi_ai_llm）实测机型是 **Marvell 88W8977 WiFi 批次**，无 factory bad block。**本次操作的设备是 Broadcom BCM43455 批次，有多个 factory bad block，这是后续所有问题的主因。**

## 2. 硬件接线

- 主板白色 JST 串口座（J31），SH1.0 转 2.54mm 杜邦 3P 线
- USB-TTL 模块 CH340，接 TX/RX/GND 三根，交叉接（模块 TXD → 音箱 RXD）
- **不接 5V/3V3 电源脚**
- 波特率 115200，8N1

接线后 `python -m serial.tools.miniterm COM5 115200` 能打印启动日志，确认 TX/RX 针脚正确。

## 3. 关键发现

### 3.1 设备硬件与文档不一致

| 项 | 文档（作者设备） | 本设备 |
|---|---|---|
| WiFi 芯片 | Marvell 88W8977 | **Broadcom BCM43455**（SDIO ID `02D0:4345`） |
| 驱动模块 | `mlan.ko` / `sd8xxx.ko` | **`dhd.ko`** |
| factory bad block | 无 | 多个（system0/system1/data 各有） |
| system0 用户态 | 2019 ROM 1.54.8 | **2023 构建**（ledserver `Sep 20 2023`） |

结论：**项目文档的所有 WiFi 命令、`dd` 写分区方案都是针对作者那台 Marvell 机器，本设备不能照搬。**

### 3.2 WiFi 驱动加载（Broadcom）

正确加载命令（在 rdinit shell 里，system0/system1 挂载到 `/mnt`）：

```sh
insmod /mnt/lib/modules/4.9.61/dhd.ko "firmware_path=/mnt/etc/wifi/fw_bcm43455c0_ag.bin nvram_path=/tmp/data/wifi/nvram.txt config_path=/tmp/data/wifi/config.txt"
echo -n 0 > /sys/module/dhd/parameters/op_mode
```

- `firmware_path` 实际是 `fw_bcm43454f_ag.bin`（驱动按 chip id 自动改）
- `op_mode=0` 是 STA，`=2` 是 AP
- 必须挂 `/data`（ubi0:data），读 `nvram.txt`、`config.txt`

### 3.3 wpa_supplicant 加载（关键坑）

`wpa_supplicant` 是被打包过的，硬编码依赖 `libmico-common.so` 去读 `/usr/share/mico/system.cfg`。在 rdinit 环境里直接跑会 `configerr` 退出。

**解决**：加三个 bind 挂载：

```sh
mount --bind /tmp/data /data
mount --bind /mnt/usr /usr
mount --bind /mnt/etc /etc
```

之后 `wpa_supplicant` 才能正常关联 WiFi。关联成功标志：`wpa_cli -i wlan0 status` 显示 `wpa_state=COMPLETED`。

### 3.4 SSH 认证（dropbear 2017.75）

- dropbear 版本老，只认 `ssh-rsa`，Windows OpenSSH 9.x 需要 `-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa`
- **known_hosts 冲突**：dropbear 每次重启生成新 host key，Windows/WSL 的 known_hosts 记着旧指纹，会触发 MITM 阻断导致 `Permission denied`。解决：`ssh-keygen -R` 清掉旧记录
- 密码登录（空 shadow）可行但不安全，且需每启动 bind

**2026-11-22 补充：服务端接受 key 但客户端不签名**

现象：`ssh -vv` 显示 `Server accepts key` 后紧跟 `we did not send a packet, disable method`，最终 `Permission denied`。这不是 key 不匹配，也不是 host key 问题。

两个独立原因：

1. **私钥带口令**。`~/.ssh/xiaomi` 是 `BEGIN OPENSSH PRIVATE KEY` + `aes256-ctr` + `bcrypt`，OpenSSH 能读出公钥去 offer，但无法解密签名，于是握手停在这一步。`ssh-keygen -y -f` 会要求口令。
2. **现代 OpenSSH 拒 SHA-1**。本地 10.3、WSL 9.6 都对 dropbear 2017.75 的 `ssh-rsa` 签名支持不完整；`PubkeyAcceptedAlgorithms=+ssh-rsa` 不足以解决。

可用解法：用 paramiko 走 SSH-RSA/SHA-1 连接（需无口令私钥）。paramiko 的 `RSAKey.from_private_key_file` 遇到加密私钥直接抛 `PasswordRequiredException`，所以要么给口令，要么重新生成一把无口令 key 并写进 `/etc/dropbear/authorized_keys`。

**`/data/dropbear/authorized_keys` 的 bind 不一定生效**：往 `/data/dropbear/authorized_keys` 追加 ECDSA 公钥后，重启设备仍然认证失败，而镜像内 `/etc/dropbear/authorized_keys` 里的 RSA 公钥被服务端正常接受。说明 `S45sshen` 的 `mount --bind` 没覆盖到 dropbear 实际读取的文件（或该 dropbear 构建不支持 ECDSA）。要改 key 以镜像内文件为准。

## 4. 操作步骤回顾（按时间）

### 4.1 串口连接

1. 装 CH340 驱动
2. PuTTY / miniterm 连 COM5 115200
3. 抓启动日志，确认 U-Boot、分区表、failsafe 状态

### 4.2 抢 U-Boot 提示符（顺序固定：先起进程，再上电）

U-Boot 倒计时只有 1 秒。一次性连发几个回车**必然落空**——突发字节几毫秒就发完，落在窗口外的概率很大。必须在上电前起一个每 30ms 发一个 `\r` 的常驻进程，然后给设备上电。

脚本 `tools/uart_bridge.ps1`，后台运行：

```powershell
& 'D:\workspace\github\xiaomi_ai_llm\tools\uart_bridge.ps1' -Port COM5 -Baud 115200 -SpamSeconds 300
```

- 收到的字节实时追加到 `.tmp-uart\uart.log`
- 检测到 `s12#` 立即停止发回车，静默 4 秒后退出并释放串口
- **串口独占**：MCP 串口工具占着 COM5 时脚本打不开，必须先 `close_port`
- 脚本退出后再用 MCP 工具或 PuTTY 发命令
- 若日志一直为空，说明设备没在输出（未上电或已停在无输出状态），断电重上电即可

进 `s12#` 后：

```sh
setenv bootargs "rootfstype=ramfs rdinit=/bin/sh console=ttyS0,115200 no_console_suspend"
imgread kernel boot0 ${loadaddr}
bootm ${loadaddr}
```

**不要 `saveenv`、`run storeboot`、`run storeargs`** —— 这些会重建 bootargs 或写 NAND env。

### 4.3 rdinit 环境搭建

```sh
mount -t proc none /proc
mount -t sysfs none /sys
mount -t tmpfs none /dev
mount -t tmpfs none /tmp
mknod /dev/null c 1 3; mknod /dev/console c 5 1
mknod /dev/urandom c 1 9; mknod /dev/random c 1 8
for i in 0 1 2 3 4 5 6; do mknod /dev/mtdblock$i b 31 $i 2>/dev/null; done
mkdir -p /mnt
mount -t squashfs /dev/mtdblock4 /mnt -o ro   # 或 mtdblock5
export PATH=/mnt/sbin:/mnt/usr/sbin:/mnt/bin:/mnt/usr/bin:$PATH
export LD_LIBRARY_PATH=/mnt/lib:/mnt/usr/lib
mkdir -p /tmp/data /data /usr /etc
mknod /dev/ubi_ctrl c 10 58 2>/dev/null
ubiattach -m 6 -d 0
mknod /dev/ubi0_0 c 242 1
mount -t ubifs ubi0:data /tmp/data
mount --bind /tmp/data /data
mount --bind /mnt/usr /usr
mount --bind /mnt/etc /etc
mount --bind /mnt/lib /lib
```

### 4.4 读镜像（正确方式）

```sh
dd if=/dev/mtdblock4 of=/tmp/system0.img bs=64k count=512
```

- 大小必须 `33554432`（32MiB = 512 × 64KiB）
- `count=512` 必加，否则读越界报 I/O error（预读越界，无害但乱）
- md5 三次一致才算读成功

### 4.5 传输镜像

Windows 侧 Python HTTP 服务器接收/发送，设备侧 curl：

```sh
# 上传到 Windows
curl --data-binary @/tmp/system0.img http://192.168.100.99:8000/
# 下载到设备
curl -o /tmp/system1_ssh_padded.img http://192.168.100.99:8002/
```

### 4.6 注入与重打包（WSL）

```sh
# 解包
unsquashfs -d s0root system0.img
# 注入 sshen + authorized_keys + rc.local
# 重打包
mksquashfs s0root out.img -comp xz -b 131072 -no-xattrs -all-root -pf pseudo.txt -noappend
# 补齐到 32MiB
dd if=out.img of=out_padded.img bs=33554432 conv=sync count=1
```

pseudo.txt 必须含字符设备节点：

```
/dev/console c 0600 0 0 5 1
/dev/null c 0666 0 0 1 3
/dev/ptmx c 0666 0 0 5 2
```

## 5. 遇到的坑（按严重程度）

### 5.1 致命坑：往已挂载的 mtdblock 写镜像（本次事故根因）

> **2026-11-22 上电复测修正**：启动日志显示 `aml_nand_bbt_check 1389 bbt is valid, reading.`，坏块表**没有损坏**，factory 坏块 `3380000`/`4060000`/`7fe0000` 每次都正确识别。所以"BBT 被破坏"的结论不成立。真正坏掉的只是两个 system 分区里的 squashfs 数据。另外 `0x7fe0000`（chip blk 1023）是 BBT 保留区，不是真坏块。

**错误 1**：写 mtdblock4 时它正挂在 `/mnt` 上，`dd of=/dev/mtdblock4` 写到已挂载设备 → 损坏 system0。

**错误 2**：改用 `nandwrite -p /dev/mtd4` 写 char 设备，但镜像来自 `dd if=/dev/mtdblock4` 是"逻辑布局"（坏块已跳过），nandwrite 期望"物理布局"（坏块留空）→ 坏块之后数据整体错位，且破坏 BBT。

**错误 3**：坏块表破坏后，mtdblock 的读写全部错位，继续用 `dd` 写 system1 也被连带损坏。

**教训**：
- 写分区前必须确认目标未挂载（`mount | grep mtdblock`）
- `dd if=/dev/mtdblockN` 得到的是**逻辑布局**（坏块已跳过），正好是 squashfs 镜像本身；写回去要用**坏块感知**的工具（`mtd -f write` / `nandwrite`）走 char 设备，它们会自动跳过坏块。不要用 `dd of=/dev/mtdblockN`。
- 真正的原因只有一个：**镜像写入时没有跳过坏块**，导致坏块之后每个块整体前移一格，坏块位置原本的数据被覆盖。BBT 本身始终是好的。

**2026-11-22 实测确认的写回方式**（已在 system1 上走通）：

```sh
mtd -f write - /dev/mtd5 < /tmp/s1.img
```

`mtd` 会打印 `Skipping bad block at 0x00040000`，这是正常行为；写完后重启，system1 能正常挂载即成功。

### 5.2 坑：udhcpc 卡死

busybox 的 `udhcpc` 无 `default.script`，拿到 IP 但不配接口，且默认无限重试会挂住串口。

解决：静态配 IP：

```sh
ifconfig wlan0 192.168.100.31 netmask 255.255.255.0 up
route add default gw 192.168.100.1 wlan0
```

### 5.3 坑：wpa_supplicant 静默退出

缺 `/usr/share/mico/system.cfg`（需 bind 挂载 /mnt/usr），且 `-f` 日志参数不被支持。用 `strace` 定位到 `configerr` 才找到根因。

### 5.4 坑：dropbear host key 0 字节

system0 里的 `dropbear_rsa_host_key` 是 0 字节空文件，直接用它启动 dropbear 报 `Bad buf_getptr`。需要先 `dropbearkey -t rsa -f` 生成。

### 5.5 坑：SSH known_hosts 冲突

每次重启 dropbear 换 host key，客户端 known_hosts 记旧指纹 → MITM 阻断 → 误判为 key 认证失败。实际 key 是对的，清 known_hosts 即可。

### 5.6 坑：device 端 TTS 需要交叉编译

`ettsc`（端侧 EdgeTTS）需要 Rust + Zig + cargo-zigbuild，目标 `arm-unknown-linux-musleabihf`。`aivs_speech_guard` 需要 Zig。本设备未完成编译（事故发生在部署前）。

### 5.7 坑：MTD 字符设备的 minor 不是分区号

最容易误导的一个。`/sys/class/mtd/mtd5/dev` 给的是 `90:10`，不是 `90:5`——**每个 MTD 分区占两个 minor（rw 和 ro）**，所以 `minor = 分区号 × 2`。

```sh
cat /sys/class/mtd/mtd5/dev     # 90:10  ← rw
cat /sys/class/mtd/mtd5ro/dev   # 90:11  ← ro
```

犯过的错：

1. `mknod /dev/mtd5 c 90 5` → 实际指向 mtd2 的 ro 设备。读能读（读到的是别的东西），写直接 `Could not open mtd device: /dev/mtd5`。
2. 用 `touch /dev/mtd5` 创建了一个**普通文件**，`mtd` 同样打不开。
3. `mknod` 默认权限是 `crw-r--r--`，没有写位，即使 minor 对了也打不开。必须 `chmod 600`。

正确做法是照着 sysfs 建节点：

```sh
for i in 0 1 2 3 4 5 6; do
  D=$(cat /sys/class/mtd/mtd$i/dev)
  mknod /dev/mtd$i c ${D%:*} ${D#*:}
  chmod 600 /dev/mtd$i
done
```

### 5.8 坑：写完用 mtdblock 读回校验会得到假失败

写完立刻 `dd if=/dev/mtdblock5 | md5sum`，md5 一定和原镜像对不上（还会伴随 `blk_update_request: I/O error, sector 65536` 这类越界报错）。原因有两条：

- mtdblock 有页缓存，之前挂载过 `/mnt` 时读过的页是**旧的**，drop_caches 也未必清干净；
- mtdblock 的读在带 factory bad block 的分区上会卡住甚至挂死。

**判据换成重启后能否挂载**：串口出现 `Booting from boot1` → `/dev/mtdblock5 is ready now.`，之后没有 `SQUASHFS error`，`ledserver` / `crond` 起来，就算写成功。要精确校验就在 U-Boot 里 `nand read` + `crc32` 对比，别在 Linux 里用 mtdblock。

### 5.9 坑：卸掉 /mnt 后 mtd 二进制的动态库也没了

`mtd` 是动态链接的 armhf 程序（解释器 `/lib/ld-linux-armhf.so.3`），而 `/lib`、`/usr`、`/etc` 都是从 `/mnt` bind 过去的。为了写分区必须 `umount /mnt`，这时 `mtd` 就跑不起来了。

解决：卸之前把整个 `/lib`（7.1M）拷到 tmpfs，并把 loader 放回 `/lib`：

```sh
mkdir -p /tmp/rl; cp -a /mnt/lib/. /tmp/rl/
cp -a /mnt/usr/lib/libubox.so* /tmp/rl/ 2>/dev/null
cp -a /mnt/sbin/mtd /tmp/mtd; cp -a /mnt/usr/bin/md5sum /tmp/md5sum
umount -l /lib; umount -l /usr; umount -l /etc; umount -l /mnt
cp /tmp/rl/ld-linux-armhf.so.3 /lib/
export LD_LIBRARY_PATH=/tmp/rl
```

之后 `/tmp/mtd` 可以直接跑，不需要挂载任何分区。

### 5.10 坑：32MiB 镜像别走 XMODEM，走 WiFi

recovery.md 里的 U-Boot XMODEM 方案只传 128KiB 缺失块，是因为串口 115200 传 32MiB 要 50 分钟。有网络时完全不需要：

```sh
# Windows 侧
python -m http.server 8000 --bind 0.0.0.0 --directory .
# 设备侧（rdinit 里已连上 WiFi）
curl -s -o /tmp/s1.img http://<host>:8000/system1_ssh_padded.img
md5sum /tmp/s1.img     # 必须和 PC 侧一致
mtd -f write - /dev/mtd5 < /tmp/s1.img
```

32MiB 走 WiFi 十几秒，写 NAND 约一分钟。

注意 `udhcpc` 在 rdinit 里会拿到租约但**不配置接口**（缺 default.script），必须手动静态配：

```sh
ifconfig wlan0 192.168.100.31 netmask 255.255.255.0 up
route add default gw 192.168.100.1 wlan0
```

Windows 防火墙默认挡 ICMP，`ping` 不通不代表网络不通，用 `curl` 测 TCP。

### 5.11 坑：uart_bridge 会卡在等 `s12#`

`tools/uart_bridge.ps1` 每发一条 `uart_in.txt` 里的命令，都要等 `s12#` 回来才发下一条，超时 120 秒。进 rdinit 后提示符是 `/ #`，它永远等不到，于是连续卡住 N×120 秒，期间 COM5 一直被占用，`stop` 文件也不管用（进程阻塞在等待循环里）。

处理：直接杀进程，再打开串口。

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*uart_bridge*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

另外 U-Boot 抢提示符的时序有讲究：**必须先把桥接跑起来并确认端口已打开，再上电**。桥接重启的空档里上电会直接漏过 1 秒窗口，设备一路引导进 Linux。

### 5.12 坑：服务端 EdgeTTS 硬依赖 ffmpeg，且 reload 会丢 PATH

`server/tts/edge_tts_client.py` 在 `__init__` 里 `shutil.which("ffmpeg")`，找不到直接 `RuntimeError` 退出。没装系统 ffmpeg 时可以借 `imageio-ffmpeg` 自带的二进制：

```powershell
.venv\Scripts\python.exe -m pip install imageio-ffmpeg
copy .venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-*.exe .venv\Scripts\ffmpeg.exe
```

还要注意两点：

- `.venv\Scripts` 必须在启动服务端时的 `PATH` 里，`shutil.which` 才找得到。
- `config.yaml` 默认 `server.reload: true`，uvicorn 的 reloader 子进程会丢掉这个临时 PATH，结果还是报缺 ffmpeg。启动时加 `--no-reload` 才是单进程。

### 5.13 坑：`mode=full` 会去下 1.42G 的 whisper 模型

本设备的路线是 `LLM_PIPELINE=native` + `TTS_ENGINE=server`——音箱自己直连 DeepSeek，服务端只负责 TTS。这时用 `--mode tts-only` 启动，跳过 ASR 和 LLM：

```powershell
$env:PATH = 'D:\workspace\github\xiaomi_ai_llm\.venv\Scripts;' + $env:PATH
.venv\Scripts\python.exe -m server.run --no-reload --mode tts-only
```

验证服务端 TTS 是否正常（注意 `/api/v1/tts` 收的是 query/form 参数，传 JSON 会 422）：

```sh
curl -X POST "http://127.0.0.1:8080/api/v1/tts?message=测试"
```

### 5.14 坑：`/data/init.sh` 缺失时自启动静默失效

`rc.local` 里的 hook 是 `[ -f "/data/init.sh" ] && sh /data/init.sh &`。`/data` 里只有 `data_init_native_first.sh` 模板、没有 `init.sh` 时，判断为假，什么都不会发生，也没有任何报错。首次部署必须先：

```sh
[ -f /data/init.sh ] || cp /data/data_init_native_first.sh /data/init.sh
chmod +x /data/init.sh
```

## 6. 正确流程（若重新来过）

1. **先完整备份所有分区**，md5 校验，确认备份可读
2. **确认坏块表（BBT）状态**，`aml_nand_bbt_check ... bbt is valid` 说明表是好的；坏的是分区里的 squashfs 数据
3. 写分区前 `mount | grep` 确认目标未挂载，并把 `mtd` 需要的 `/lib` 拷到 tmpfs
4. 用 `sysfs` 的 `dev` 建 MTD char 节点（`minor = 分区号 × 2`），`chmod 600`
5. 镜像走 WiFi 传到 `/tmp`，`md5sum` 与 PC 一致后才写
6. `mtd -f write - /dev/mtd5 < /tmp/s1.img`，它会自动跳过坏块
7. **校验用重启挂载，不要用 mtdblock 读回 md5**
8. 启动后立刻确认 `/data/init.sh` 存在且可执行，否则自启动会静默失效

## 7. 当前未完成事项

- [x] 恢复 system1（`mtd -f write` 写入注入版镜像，重启无 SQUASHFS 错误）
- [x] 配置 `/data/native_first.env`（`SERVER`/`TTS_SERVER` 指向 Windows、填 DeepSeek key，备份 `.bak`）
- [x] 验证自启动（`/data/init.sh` 补齐后重启，客户端自行拉起并访问服务端）
- [x] 跑通第一轮对话（服务端日志出现 `POST /api/v1/tts/stream 200`）
- [ ] 编译 ettsc（端侧 TTS，可脱离 PC 朗读）
- [ ] 编译 aivs_speech_guard（boot1 快速拦截）
- [ ] SSH 稳定可用（当前受私钥口令与现代 OpenSSH 弃用 SHA-1 限制，暂用串口）
- [ ] 追问链路验证（`FOLLOWUP_ENABLED`、native_live 组件）

## 8. 参考文件

- 恢复信息：`recovery.md`
- 项目文档：`docs/`（作者设备，Marvell）
- 关键 runbook：`docs/runbooks/boot0-ssh.md`
- 硬件参考：`docs/reference/hardware.md`
- 快速上手：`docs/getting-started/quickstart.md`
