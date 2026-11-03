# Megrez 排障手册：从"看起来还活着"到"确定要不要按复位"

日期：2026-10-30

这块板子的失效模式几乎都不是"黑屏"，而是**部分还活着**。桌面在动、鼠标能用，
但机器已经做不了正事了。下面按顺序判断，能省掉很多次无谓的复位。

## 1. 先分清三种"活着"

| 现象 | 含义 | 依据 |
| --- | --- | --- |
| 桌面在动、鼠标键盘有反应 | **只说明 Xorg/窗口管理器还在内存里**，画图和输入不碰磁盘 | 2026-10-30 两次 eMMC 卡死时都是这样 |
| 串口回显 bash 内置命令（`echo`、`[`、`cd`） | 内核和 shell 活着 | |
| 能启动**没缓存过**的程序（例如 `/usr/sbin/reboot`） | 文件系统也活着 | 这一条才是"健康" |

一条命令判断：

```sh
/usr/sbin/reboot --help >/dev/null 2>&1; echo rc=$?
```

`rc=0`（或正常打印帮助）→ 系统健康；`Input/output error` / `cannot execute
binary file` / `Exec format error` → **文件系统层坏了**，别再用 GUI 判断。

## 2. 文件系统坏了还想重启：按顺序试

1. `echo b > /proc/sysrq-trigger` —— 新内核（2026-10-30 起）实现，只需 bash
   内置命令，不启动程序、不碰磁盘。
2. `/usr/sbin/reboot` —— 若二进制恰好还能读出来。
3. `kill -s RTMIN+5 1` —— systemd 的重启信号；systemd 被冻结时无效。
4. 以上都不行就**只能物理复位**。

已经确认无效、不要重复的手段：`/proc/sysrq-trigger`（旧内核没有此文件）、
`python3 -c ctypes...`（`_ctypes.so` 读坏块）、USB 串口 DTR/RTS 当复位线
（这块板子没接线）、内核 panic（未 `asterinas.panic_reboot=1` 时是 halt 不是重启）。

## 3. 明确会触发故障的操作（别做）

- `systemctl stop asterinas-desktop-m5`：**整机挂死**（TTY 接管）。要静音只停
  `asterinas-browser-web`。
- 对挂载中的根分区直接 `dd` 写镜像而不先 `mount -o remount,ro /`：脏页会在写完后
  盖掉新镜像，读回校验必然不符。
- 刷写时把 `dd` 和 `reboot` 分成两条命令：写完页缓存与磁盘不一致，连 `reboot`
  都执行不了。
- 在 U-Boot 计数期间往串口打字：会停住 autoboot，落到 `=>` 提示符。

## 4. 复位之后的标准动作

```sh
# 控制台桥
tools/riscv/board/megrez_console.py bridge /dev/ttyUSB0 --log /tmp/board2.log --fifo /tmp/board2.cmd &

# 逐条短命令验收：date -u / curl https://www.baidu.com/ / tail 导航日志
```

刷写新镜像（镜像里必须已经含内核）：

```sh
sudo python3 tools/riscv/board/prepare_board_image.py \
  --image target/debian-riscv/browser-web/rootfs/debian-root.ext2 \
  --kernel target/osdk/aster-kernel-osdk-bin.Image
python3 tools/riscv/board/megrez_flash_rootfs.py \
  --image target/debian-riscv/browser-web/rootfs/debian-root.ext2 \
  --url http://<host>:18090/
```

## 5. 证据保留

崩溃一次就会留下证据：控制台日志 `/tmp/board2.log`、
`ASTERINAS_UNCLEAN_SHUTDOWN`（2026-10-30 起由 `asterinas-boot-state.service` 打）、
`ASTERINAS_FIREFOX_WEB_PROFILE_ROTATED`（profile 因坏名字被轮换）。
