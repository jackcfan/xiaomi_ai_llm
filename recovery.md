# 设备恢复信息

> 状态：system1 已恢复并刷入注入版镜像（2026-11-22 实测通过，重启可挂载、自启动生效、LLM+TTS 全链路跑通）；system0 已刷入 `system0_ssh_hook_padded.img`（= `system0_ssh_padded.img` + system1 同款 rc.local 钩子，md5 `f0e7a623ceac9dd9166d7d67292a477b`），`boot_part=boot0` 已 saveenv，当前从 system0 启动。
> 生成时间：2026-11-22
> 最后更新：2026-11-22（system1 修复完成，补 Linux 内写回快路径）

## 1. 设备当前状态

| 项 | 状态 |
|---|---|
| 串口 UART（COM5，CH340） | ✅ 完好，115200 8N1 |
| U-Boot（`s12#` 提示符） | ✅ 完好，`boot_part=boot1` |
| `/data`（mtdblock6，UBI/UBIFS） | ✅ 完好 |
| system0（mtdblock4） | ✅ 已修复（2026-11-22 写入 `system0_ssh_hook_padded.img`；`Booting from boot0` → `/dev/mtdblock4 is ready now.`，dropbear 22 端口可达） |
| system1（mtdblock5） | ✅ 已修复，回读 crc32 `46df94d5` = `system1.img`，正常启动 |
| 正常启动 | ✅ 当前 `boot_part=boot0` 起 system0；system1 仍完好可切回 |

`/data` 内已有内容：

- `native_first_client.sh`、`native_first.env`、`native_first.env.example`
- `vad_record.sh`、`data_init_native_first.sh`
- `/data/dropbear/authorized_keys`（用户公钥，398 字节）
- `/data/ssh_en`（值 1）

## 2. 完整镜像备份（Windows 主机）

所有文件在 `C:\Users\jackcfan\AppData\Local\Temp\`：

| 文件 | md5 |
|---|---|
| `system0.img`（原始，未注入） | `9ae8566181484c3ad1cc7bce6615e0aa` |
| `system0_ssh_padded.img`（注入 sshen + key） | `8732c4b0278d027a3503fe2aff1e990d` |
| `system1.img`（原始，未注入） | `8caee839620d7ba87405ab7b9b62ca9c` |
| `system1_ssh_padded.img`（注入 sshen + key + rc.local） | `24dcf15e36786798bd17d24a2c6ea892` |

WSL Ubuntu 内也有副本：

```
~/system0.img
~/system1.img
~/system1_ssh_padded.img
~/s0root/            # system0 解包目录
~/s1root/            # system1 解包目录（已注入）
```

原始镜像 squashfs 参数（重打包必须一致）：

```
squashfs 4:0, xz, block size 131072, no-xattrs, no always-fragments
```

system0 原始 superblock：`Filesystem size 31534531 bytes`，`inodes 2018`
system1 原始 superblock：`Filesystem size 31659559 bytes`，`inodes 1984`

## 3. 硬件关键信息

```
NAND: Samsung FS33ND01GS108TFI0（1Gbit SLC，128MiB，page size 2048，OOB size 64）
SoC:  Amlogic A113X（aarch64 内核 4.9.61，armhf 用户态）
型号: 小米 AI 音箱 MDZ-25-DA / S12A
WiFi: Broadcom BCM43455（SDIO ID 02D0:4345）—— 注意：项目作者那台是 Marvell 88W8977
```

分区表（U-Boot 启动日志）：

```
mtd0 bootloader 0x00000000-0x00200000
mtd1 tpl        0x00800000-0x01000000
mtd2 boot0      0x01000000-0x01800000  （kernel + initramfs）
mtd3 boot1      0x01800000-0x02000000  （kernel + initramfs）
mtd4 system0    0x02000000-0x04020000
mtd5 system1    0x04020000-0x06040000
mtd6 data       0x06040000-0x08000000
```

Factory bad blocks（启动日志记录）：

```
NAND bbt detect factory Bad block at 3380000   → system0 内（逻辑 blk 156）
NAND bbt detect factory Bad block at 4060000   → system1 内（物理 blk 2 / blk 515）
NAND bbt detect factory Bad block at 7fe0000   → data 内
```

## 4. 损坏根因

### 4.1 实际根因（2026-11-22 上电复测确认）

启动日志显示 `aml_nand_bbt_check 1389 bbt is valid, reading.`，三次 factory 坏块（`3380000` / `4060000` / `7fe0000`）每次都被正确识别。**坏块表没有损坏。**

真正的原因只有一个：**镜像写入时没有跳过坏块**，导致坏块之后的每一个块整体向前平移一格（左移），坏块位置原本的那一块数据被覆盖丢失。

实测证据（system1，逐块 crc32 与 `system1.img` 比对）：

```
镜像内存块  CRC32       对应原始镜像
blk 0      a83f772c   = blk 0   ✓
blk 1      2b1d8e94   = blk 1   ✓
blk 2      3592f046   = blk 3   ← 原始 blk 2 丢失
blk 3      8dd3cdf7   = blk 4   ✓
...        之后全部平移 -1
```

也就是说 squashfs 超级块（在块 0）是完好的，所以 `md` 看起来正常，但 inode/目录索引在坏块之后整体错位，于是内核报 `SQUASHFS error: unable to read id index table` / `unable to read inode lookup table` 并挂载失败。

### 4.2 此前错误结论的更正

| 曾经的判断 | 实际情况 |
|---|---|
| "BBT 被破坏了" | 错。BBT 完好，每次启动都 `bbt is valid`。factory 坏块标记烧在 OOB 里，数据擦除擦不掉 |
| "需要物理布局镜像（坏块位置留空）" | 不适用。Amlogic mtd 驱动按密集布局寻址并自动跳过坏块，U-Boot `nand read/write` 同样如此 |
| "0x7fe0000 是 data 分区里的坏块" | 不完全。该地址是 chip blk 1023，即芯片最后一块，是 BBT 保留区，不是真正的数据坏块 |

**唯一可靠的判据是写完回读 crc32 与原始镜像比对**，不要靠布局推算。

## 5. 恢复方法（已在 system1 上实测走通）

整个修复在 U-Boot 内完成，不需要 Linux、不需要网络。核心思路：**先在内存里把错位的数据挪回正确位置，只传真正缺失的那一块，校验通过后再擦写 NAND。**

### 5.0 前置：抢 U-Boot 提示符

U-Boot 倒计时只有 1 秒，一次性突发回车必然落空。必须先起一个每 30ms 发一个 `\r` 的后台进程，再给设备上电。

```powershell
& 'D:\workspace\github\xiaomi_ai_llm\tools\uart_bridge.ps1' -Port COM5 -Baud 115200 -SpamSeconds 1800
```

`tools/uart_bridge.ps1` 的行为：

- 收到的串口字节实时追加到 `.tmp-uart\uart.log`
- 检测到 `s12#` 立即停止发回车
- 从 `.tmp-uart\uart_in.txt` 读取要发的命令，**每条命令等到 `s12#` 回来才发下一条**
- 串口独占，MCP 串口工具占着 COM5 时会打不开，先用完再起脚本；脚本运行时反过来不能再用 MCP
- 写 `.tmp-uart\stop` 空文件即可让脚本释放串口

U-Boot 环境（实测确认）：

```
baudrate   = 115200     ← 只有 115200 / 57600 可用
                          230400 / 460800 / 921600 均报 "not supported"
loadaddr   = 1080000
loadx 可用   ← XMODEM-CRC
crc32 可用
nand read / nand write / nand erase.part 可用（自动跳过坏块）
```

### 5.1 定位错位点（只读，不改 NAND）

```sh
# 读出整个分区到内存
nand read 0x1000000 system1 0x2000000

# 整分区 crc32，与 PC 侧原镜像对比
crc32 0x1000000 0x2000000
```

PC 侧参考值：

| 镜像 | crc32 | md5 |
|---|---|---|
| `system1.img` | `0x46df94d5` | `8caee839620d7ba87405ab7b9b62ca9c` |
| `system0.img` | `0xe8607b1c` | `9ae8566181484c3ad1cc7bce6615e0aa` |
| `system0_ssh_padded.img` | `0x840d77d1` | `8732c4b0278d027a3503fe2aff1e990d` |
| `system1_ssh_padded.img` | `0xa4d0a316` | `24dcf15e36786798bd17d24a2c6ea892` |

对不上就用逐块 crc32 找错位点（脚本生成 256 条命令，见 `tools/` 里用过的循环）：

```sh
crc32 0x1000000 0x20000    # blk 0
crc32 0x1020000 0x20000    # blk 1
...
```

坏块位置：

| 分区 | chip blk | 分区内相对块 | 字节地址 |
|---|---|---|---|
| system0 | 412 | 156 | `0x3380000` |
| system1 | 515 | 2 | `0x4060000` |

分区各 257 块 = 256 可用 + 1 坏块，正好构成 32MiB 镜像。

### 5.2 在内存里右移复原

数据整体左移一格，所以要从**高地址往低地址**逐块右移，避免覆盖。全部落在 `0x1000000`–`0x3000000` 内：

```sh
nand read 0x1000000 system1 0x2000000

# i 从 255 递减到 2：把 blk(i-1) 搬到 blk(i)
cp.b 0x11fe0000 0x1200000 0x20000
...
cp.b 0x1020000  0x1040000 0x20000
```

生成这批命令的 PowerShell 片段：

```powershell
for ($i = 255; $i -ge 2; $i--) {
    "cp.b 0x{0:x} 0x{1:x} 0x20000" -f (0x1000000 + ($i-1)*0x20000), (0x1000000 + $i*0x20000)
}
```

移动完先验：`crc32 0x1060000 0x1fa0000` 应等于 `0x380140fd`（原镜像 `0x60000`–`0x2000000` 的 crc32）。
此时只有 blk 2 还是旧内容，需要单独补。

### 5.3 用 XMODEM 补上缺失的那一块

缺失的是原镜像的 blk 2（`0x40000`–`0x60000`），要放到内存的 `0x1040000`。

**地址必须按 `0x20000` 块对齐。** `0x1004000` 只是 buffer 内 16KiB，不是块 2 的边界；写错会破坏 blk 0 尾部 / blk 1 / blk 2 头部，这是本次调试里踩过的坑。

先停掉 bridge 释放串口，再：

```powershell
python tools\xmodem_send.py system1.img 0x40000 0x20000 0x1040000
```

`tools/xmodem_send.py` 参数：`<镜像路径> <文件内偏移> <长度> <U-Boot 目标地址>`。它自动处理 U-Boot 的 `loadx` 回显（不能指望第一个字节就是握手 `C`），失败自动重传。

成功后 U-Boot 打印：

```
xyzModem - CRC mode, 1024(SOH)/0(STX)/0(CAN) packets, 0 retries
## Total Size      = 0x00020000 = 131072 Bytes
```

### 5.4 写 NAND 前必须整体校验

重新起 bridge，然后：

```sh
crc32 0x1040000 0x20000          # 应为 0x7f224913（原镜像 blk 2）
crc32 0x1000000 0x2000000        # 应为 0x46df94d5（整镜像）
```

**两个都对上才允许写。** 对不上用逐块 crc32 定位，不要盲写。

### 5.5 擦写与回读验证

```sh
nand erase.part system1
nand write 0x1000000 system1 0x2000000
nand read 0x1000000 system1 0x2000000
crc32 0x1000000 0x2000000
```

回读 crc32 必须仍等于 `0x46df94d5`。写的时候 U-Boot 会打印 `Skip bad block 0x04060000`，这是正常行为。

### 5.6 切启动分区并重启

```sh
setenv boot_part boot1
saveenv
reset
```

启动成功标志：串口出现 `Booting from boot1` → `/dev/mtdblock5 is ready now.`，**没有** `SQUASHFS error`，随后 `ledserver` / `crond` / `micocfg` 起来，灯亮。

### 5.7 system0 怎么修

完全同样的流程，替换参数：

| 项 | system1 | system0 |
|---|---|---|
| 分区名 | `system1` | `system0` |
| 坏块相对块 / 字节 | blk 2 / `0x4060000` | blk 156 / `0x3380000` |
| 目标镜像 | `system1.img` | `system0_ssh_padded.img` |
| 整镜像 crc32 | `0x46df94d5` | `0x840d77d1` |
| 缺失块的镜像内偏移 | `0x40000` | `0x1380000` |
| 缺失块的内存目标地址 | `0x1040000` | `0x2380000` |
| 右移范围 | `i` 从 255 到 2 | `i` 从 255 到 157 |
| 右移后前缀校验（`crc32 0x1000000 0x1380000`） | — | 应等于 `0x87a25675` |
| 启动变量 | `boot_part=boot1` | `boot_part=boot0` |

**更好的做法**：先让 system1 起来并打通网络/SSH，然后在 Linux 里直接用 `nandwrite` 写 system0，省一次 32MiB 串口传输。

### 5.8 快路径：进 Linux，WiFi 传镜像 + mtd 字符设备写回（2026-11-22 实测，约 5 分钟）

5.1–5.6 那套 U-Boot + XMODEM 的前提是"没有 Linux、没有网络"。只要能进 rdinit shell 且 WiFi 能起来，直接写更快，整套约 5 分钟。

**第一步：进 rdinit shell**（同 5.0，bootargs 用 `rdinit=/bin/sh`），然后建节点、挂 system1：

```sh
mount -t proc none /proc; mount -t sysfs none /sys
mount -t tmpfs none /dev; mount -t tmpfs none /tmp
mknod /dev/null c 1 3; mknod /dev/console c 5 1; mknod /dev/urandom c 1 9
mkdir -p /mnt /tmp/data /data
mknod /dev/mtdblock5 b 31 5          # 块设备：major 31，minor = 分区号
mount -t squashfs /dev/mtdblock5 /mnt -o ro
export PATH=/mnt/sbin:/mnt/usr/sbin:/mnt/bin:/mnt/usr/bin:$PATH
export LD_LIBRARY_PATH=/mnt/lib:/mnt/usr/lib
```

**第二步：挂 `/data`、起 WiFi**

```sh
mknod /dev/ubi_ctrl c 10 58; mknod /dev/ubi0_0 c 242 1
ubiattach -m 6 -d 0
mount -t ubifs ubi0:data /tmp/data
mount --bind /tmp/data /data
mount --bind /mnt/usr /usr; mount --bind /mnt/etc /etc   # wpa_supplicant 需要

insmod /mnt/lib/modules/4.9.61/dhd.ko "firmware_path=/mnt/etc/wifi/fw_bcm43455c0_ag.bin nvram_path=/tmp/data/wifi/nvram.txt config_path=/tmp/data/wifi/config.txt"
echo 0 > /sys/module/dhd/parameters/op_mode
ifconfig wlan0 up
wpa_supplicant -i wlan0 -c /tmp/wpa.conf -B
```

`/tmp/wpa.conf` 自己写（`/data/wifi/wpa_supplicant.conf` 里的 SSID 可能已经不存在）：

```sh
printf 'ctrl_interface=/var/run/wpa_supplicant\nupdate_config=1\nnetwork={\n ssid="<SSID>"\n psk="<密码>"\n}\n' > /tmp/wpa.conf
```

`udhcpc` 在 rdinit 里会拿到租约但**不配置接口**（缺 default.script），必须静态配：

```sh
ifconfig wlan0 192.168.100.31 netmask 255.255.255.0 up
route add default gw 192.168.100.1 wlan0
```

（Windows 防火墙默认挡 ICMP，`ping` 不通不代表网络不通，用 `curl` 验。）

**第三步：下载并校验**

```sh
# PC 侧
python -m http.server 8000 --bind 0.0.0.0 --directory .
# 设备侧
curl -s -o /tmp/s1.img http://<PC_IP>:8000/system1_ssh_padded.img
md5sum /tmp/s1.img      # 必须和 PC 侧 md5 完全一致
```

**第四步：卸挂载前把 mtd 依赖搬到 tmpfs**

`mtd` 是动态链接的 armhf 程序（解释器 `/lib/ld-linux-armhf.so.3`），而 `/lib` 是 `/mnt/lib` 的 bind。卸掉 `/mnt` 后它就跑不起来：

```sh
mkdir -p /tmp/rl; cp -a /mnt/lib/. /tmp/rl/; cp -a /mnt/usr/lib/libubox.so* /tmp/rl/ 2>/dev/null
cp -a /mnt/sbin/mtd /tmp/mtd; cp -a /mnt/usr/bin/md5sum /tmp/md5sum
umount -l /lib; umount -l /usr; umount -l /etc; umount -l /mnt
cp /tmp/rl/ld-linux-armhf.so.3 /lib/
export LD_LIBRARY_PATH=/tmp/rl
```

**第五步：按 sysfs 建 MTD 字符节点再写**

**minor 不是分区号**：每个 MTD 分区占两个 minor（rw / ro），`minor = 分区号 × 2`。

```sh
cat /sys/class/mtd/mtd5/dev        # 90:10  ← rw，写这个
cat /sys/class/mtd/mtd5ro/dev      # 90:11  ← ro

D=$(cat /sys/class/mtd/mtd5/dev)
rm -f /dev/mtd5
mknod /dev/mtd5 c ${D%:*} ${D#*:}
chmod 600 /dev/mtd5                # mknod 默认无写位，不改打不开

/tmp/mtd -f write - /dev/mtd5 < /tmp/s1.img
```

打印 `Skipping bad block at 0x00040000` 属正常，工具会自动跳坏块。32MiB 走 WiFi 十几秒，写 NAND 约一分钟。

**第六步：验证**——**不要用 mtdblock 读回 md5**（页缓存陈旧，必然对不上，还会报 `blk_update_request: I/O error`）。直接重启，看串口是否出现 `Booting from boot1` → `/dev/mtdblock5 is ready now.` 且**没有** `SQUASHFS error`。



## 6. 实测踩过的坑（下次别再犯）

1. **`loadx` 目标地址按 `0x20000` 对齐。** 写成 `0x1004000` 会把数据塞进 blk 0 尾部 + blk 1 + blk 2 头部，导致前三个块 crc 全错，且每次测都不同，非常迷惑。
2. **`cp.b` 目标地址超过 `0x4000000` 会崩。** 触发 `"Synchronous Abort" handler, esr 0x96000010` 并直接复位。可用内存约 `0x1000000`–`0x3000000`。
3. **`nand read` 长度小于整分区时会多读进坏块。** 例如 `nand read 0x1000000 system1 0x40000` 会把坏块数据写到你缓冲区后一格的位置，破坏刚补好的块。只读头两块的场景要么读到整分区，要么补完立刻写。
4. **波特率提不上去。** 这版 U-Boot 只支持 115200 / 57600，`setenv baudrate 921600` 直接报 `not supported`。32MiB 走 115200 约 50 分钟，所以用 5.2 + 5.3 的"内存平移 + 只传 128KiB"方案，整套约 10 分钟。
5. **bridge 必须一条命令等一个提示符。** 连续灌命令会在长命令（如 32MiB 的 `nand read`）执行期间丢字符，日志停在半行。
6. **不要 `nand scrub`。** 会擦掉 factory 坏块标记，那才是真变砖。
7. **不要 `dd` 写正在挂载的 mtdblock。** 这是最初损坏的直接原因。
8. **MTD 字符设备 minor = 分区号 × 2。** `mknod /dev/mtd5 c 90 5` 实际指向 mtd2 的只读设备：读能读（读到的是别的东西），写直接 `Could not open mtd device`。以 `/sys/class/mtd/mtdN/dev` 为准。
9. **`touch /dev/mtd5` 会创建一个普通文件**，`mtd` 同样打不开；`mknod` 默认 `crw-r--r--` 没有写位，要 `chmod 600`。
10. **mtdblock 读回校验不可信。** 写过之后页缓存是旧的，md5 必然对不上；带坏块的分区上 `dd if=/dev/mtdblockN` 还会卡死。判据只有一个：重启后能否挂载。
11. **卸挂载后 mtd 跑不起来。** 它依赖 `/lib/ld-linux-armhf.so.3` 和 `/mnt/lib` 的一堆 so，卸前先把整个 `/lib` 拷到 tmpfs 并把 loader 放回 `/lib`（见 5.8）。
12. **32MiB 不值得走串口。** XMODEM 只用来补单个缺失块；有网络时整镜像走 WiFi + `mtd -f write`，5 分钟搞定。
13. **`uart_bridge` 会卡在等 `s12#`。** 它的发命令逻辑要求每条命令后回到 `s12#`，而 rdinit 提示符是 `/ #`，于是每条卡满 120 秒，期间 COM5 一直被占用，写 `stop` 文件也没用（进程阻塞在等待循环）。直接杀进程：`Get-CimInstance Win32_Process | ? { $_.CommandLine -like '*uart_bridge*' } | % { Stop-Process -Id $_.ProcessId -Force }`。
14. **抢 U-Boot 的时序不能错。** 必须先把桥接跑起来、确认串口已打开，**再**上电。桥接重启的空档里上电会直接漏过 1 秒窗口，设备一路引导进 Linux（此时只能靠 `S12A login:` 但那是厂商 DSA 校验，登不进去）。

## 7. 后续步骤

system1 已完成，下列各项 2026-11-22 实测通过：

- [x] 验证 system1 能挂载（rdinit 内 `mount -t squashfs /dev/mtdblock5 /mnt -o ro` 返回 0）
- [x] 刷入注入版镜像（`system1_ssh_padded.img`，含 `S45sshen` + rc.local hook），重启无 `SQUASHFS error`
- [x] 客户端与配置在 `/data`，无需重新上传
- [x] 补齐 `/data/init.sh`（之前只有 `data_init_native_first.sh` 模板，自启动静默失效）
- [x] `/data/native_first.env` 填 DeepSeek key、`SERVER`/`TTS_SERVER` 现为 NAS `http://192.168.100.100:8080`（2026-11-22 rdinit 内读 `/data/native_first.env` 确认，`TTS_ENGINE=server`），备份为 `.bak`
- [x] 自启动验证：重启后客户端自行拉起，PC 端日志出现设备 `GET /` 与 `POST /api/v1/tts/stream 200`

未完成：

- [x] system0 修复：rdinit + WiFi 下载 + `mtd -f write - /dev/mtd4`（mtd4 = `90:8`），跳过坏块 `0x01380000`，写后 system1 照常从 boot1 启动
- [x] system0 补 rc.local 钩子（WSL 内 `wsl -u root` 解包 `system0_ssh_padded.img` → 拷 system1 的 `/etc/rc.local` → `mksquashfs -comp xz -b 131072 -no-xattrs -all-root` → `truncate -s 33554432`），重刷并从 boot0 启动验证
- 注：initramfs 按 NAND 里保存的 `boot_part` 选 rootfs（`curr_boot is bootN`），只 `imgread kernel boot0` 不 saveenv 仍会挂 system1；`storeboot` 自带 `saveenv`。`/etc/init.d/done` 在启动成功后把 `boot_failcnt` 清 0，否则软重启会被 `boot_failed` 切到另一分区
- 切回 system1：U-Boot 里 `setenv boot_part boot1; saveenv; reset`
- [x] system0 上语音控制/问答无响应（2026-11-22 修复）：客户端按根分区区分方案，mtdblock4 默认走 dsnoop + `libxaudio_engine.so` 补丁。这台机两分区 ROM 几乎相同（system0 1.76.54 / system1 1.76.53，`libxaudio_engine.so` md5 相同），dsnoop 方案下系统 mipns `AudioRecord_Init: fail snd_pcm_open = -16` 后 signal 6 崩溃循环，aivs 只有 `Wakeup` → `Finish`。修复：`/data/native_first.env` 加 `NATIVE_ROOT_PROFILE=system1`（客户端新增该开关，system0 也走原生路径；备份 `native_first.env.pre_profile`）。另：原补丁偏移 700428 在 S12A 库里是机器码，客户端已改为校验 `default ` 后再打补丁（S12A 为 1005120）
- [x] 问答类全部交给 DeepSeek（2026-11-22）：S12A 上 `nlp_result_get` 为空、instruction.log 无 domain，改为按最终识别文本反向判断。`/data/native_first.env` 加 `LLM_ROUTE_ALL_QA=1`，不命中 `NATIVE_KEEP_QUERY_PATTERNS`（控制/时间/天气/播放/闹钟等关键词，内置默认）即直转 LLM；备份 `native_first.env.pre_qa`、`native_first_client.sh.pre_qa`
- [x] 问答转接无漏音（2026-11-22）：`/data/aivs_speech_guard`（`device/aivs_guard`，zig 交叉编译 aarch64-musl 静态）新增第 7 参数保留表达式，最终 ASR 文本不命中即暂停同 dialog 的原生 Speak；实测听不到小爱开头。备份 `native_first_client.sh.pre_guard`
- [x] LLM 播报可被唤醒打断（2026-11-22）：`LLM_BARGE_IN=1`（默认开）。此固件播报中再唤醒只发 `think`（无 `WuW`），回答走整段 `tts_play_text` 路径。wakeup hook 在 busy 且 `/tmp/native_first_llm_playing` 存在时杀客户端后代的 curl/aplay/ettsc，立即放开冻结的 mediaplayer 并静音清掉旧队列，再把这次 think 交给客户端按原规则路由。备份 `native_first_client.sh.pre_barge`
- 排障技巧：SSH 不可用时，可在 rdinit 里放一个轮询 PC 的临时命令代理并由 `/data/init.sh` 拉起，正常运行态下读 `/tmp/native_first_client.log`、`/tmp/log/messages`；用完删除
- 注：rdinit 里可直接 `cp /data/wifi/wpa_supplicant.conf /tmp/wpa.conf`，厂商 wpa_supplicant 能用其中加密的 `aes:` psk 连网
- [ ] SSH 稳定可用：镜像内 RSA 公钥服务端接受，但现代 OpenSSH 拒 SHA-1 签名，且私钥带口令；`/data/dropbear/authorized_keys` 的 bind 实测没覆盖到 dropbear 实际读取的文件，改 key 要以镜像内 `/etc/dropbear/authorized_keys` 为准
- [ ] 服务端 EdgeTTS 依赖 ffmpeg，未装系统版时用 `imageio-ffmpeg` 自带二进制放到 `.venv/Scripts`；启动须 `--no-reload`（reloader 子进程会丢临时 PATH），并用 `--mode tts-only` 避开 1.42G whisper 下载

## 8. 联系信息

- 项目仓库：https://github.com/open-speaker-llm/xiaomi_ai_llm
- 项目作者设备：Marvell 88W8977（无坏块），本项目设备：Broadcom BCM43455（多个 factory bad block）
- 项目文档假设前提：`docs/runbooks/boot0-ssh.md` 的 `dd` 方案适用于干净坏块映射的设备
