# Asterinas/Megrez Debian 日用验收剧本（2026-10-29）

> 目标：把“板端 Debian 能不能日用”从零散结论变成一条按顺序执行、每步可机器判定的剧本。
> 适用：Milk-V Megrez（EIC7700）+ Asterinas + Debian trixie（p2，`ASTER_BROWSERWEB`）。
> 不适用：x86 Debian（用 `tools/x86/debian/acceptance_gate.sh`）；QEMU 门禁（用
> `tools/riscv/` 下的对应 gate）。本剧本只做板端运行时验收，不改内核。
>
> 纪律：**不刷 p2、不改 p1、不 push**；启动项由操作员选择，验收过程必须有人值守；
> 出现挂起时用物理 reset，不要指望自动恢复定时器（本剧本明确禁止它）。

## 0. 一页结论

```text
主机串口 115200 8N1（录制到 /tmp/megrez-daily-acceptance.serial.log）
  → 上电 / reset
  → U-Boot 选 asterinas-daily 条目
  → 串口 shell（root debug 或 asterinas autologin）
  → sh /var/tmp/daily-acceptance.sh
  → 看 ACC_SUMMARY p0=pass|fail p1=pass|fail
```

通过口径：

- **P0**（必须全 `ok`）：启动参数契约、systemd local-fs/multi-user、根文件系统可写、
  `/proc` 关键字段、有线 IPv4/默认路由、USB 键鼠、帧缓冲与 1920×1080、GUI 应用存活 >10 s。
- **P1**（允许失败但必须记录）：`systemctl is-system-running` 严格 `running`、DNS、
  外网 HTTP。当前镜像的 DNS/外网是**已知失败**，不能写成通过。
- 只要 P0 有一项 `fail`，本次验收判 **不通过**；P1 失败写入“已知缺口”，不计入 P0。
- 报告结论应写成“P0 通过；P1 已知失败：DNS/外网/strict running”，
  **不要**写成“全部通过”。

本剧本**不覆盖**：内核 ktest（独立基线最近为 777/0，另行复核）、SDHCI 独立门禁
（`tools/riscv/megrez_sdhci_gate.py`）、Firefox 页面渲染/视频性能、apt 全量安装。
它只回答一个问题：**这块板现在能不能按 Debian 日常方式用起来**。

## 1. 前置

### 1.1 硬件与接线

| 项 | 要求 | 现场检查 |
|---|---|---|
| 串口 | 板载调试 UART 经 FTDI USB-UART 接到主机；115200 8N1、无流控；主机用户在 `dialout` 组 | `ls -l /dev/serial/by-id/`；`id -nG \| tr ' ' '\n' \| grep -qx dialout` |
| 显示 | 板载 HDMI0 接显示器；U-Boot 已初始化 **1920×1080** 固件帧缓冲；显示器输入源选 HDMI | 上电后 U-Boot/内核串口应有 `ASTERINAS_FRAMEBUFFER_INIT status=found ... width=1920 height=1080` |
| 输入 | USB 键盘 + USB 鼠标插在板载 USB 口；所用 DTB 含 `/chosen/asterinas,usb-host`（每日 DTB 已修补） | 串口应出现 `Selected DWC3 USB host`、`USB boot keyboard registered`、`USB boot mouse registered` |
| 电源 | 可物理断电/复位；操作员全程在场 | 无 `asterinas.reboot_after` 时这是唯一兜底 |

> 显示链是 U-Boot 交接的 firmware framebuffer（`/dev/fb0`），不是原生 EIC7700 DRM/HDMI；
> 本剧本不验收 GPU 加速或原生分辨率。显示器本身可以是 2560×1600，但 guest 仍是 1920×1080。

### 1.2 镜像与启动项

以 2026-10-28 已安装的 `browser-web` 单镜像为准（PR #227 分支的
`docs/porting/board-image-current.md`，commit `eeba8dc89`）：

| 位置 | 内容 | 身份（记录用） |
|---|---|---|
| p2 | Debian trixie ext2，LABEL `ASTER_BROWSERWEB`，约 3 GiB | `sha256 3b5807511393c1be65f9c3461504e6db45565b3cff309b6e37ef7bc4249ff34d`（安装记录值，以板上实测为准） |
| p1 `/boot` | RockOS 恢复文件 + 一个 `asterinas-daily` 启动项 | `kernel-daily.Image` / `stage1-daily.cpio` / `megrez-daily.dtb`（文件名以实际 extlinux 条目为准） |
| p3 | RockOS 恢复系统 + 一个 Asterinas generation | 不动 |

启动项要求：

- 选 `asterinas-daily`（每日镜像条目），**不要**选 Basic/Probe，也不要用旧的
  `--volatile-home` 桌面门禁条目做日用验收。
- 记录启动产物身份：在 U-Boot 里对 kernel/stage1/DTB 做 `crc32`，或从板上启动日志记录
  SHA-256/CRC32；没有身份记录的验收结果不可比较。
- 每日镜像的串口 shell 可能是 `root@asterinas-debug:/#`（isolated-root 调试控制台），
  也可能是 `asterinas@...$`（console autologin）。脚本对两种都兼容；先执行 `id` 记录。

### 1.3 启动参数硬性要求（**不含 `asterinas.reboot_after`**）

| 参数 | 要求 | 原因 / 证据 |
|---|---|---|
| `asterinas.reboot_after` | **禁止出现** | 到点直接 SBI 冷重启，**不 sync、不卸载、不跑 userspace 关机**（`book/src/kernel/linux-compatibility/kernel-parameters.md:248-266`）。它会在 10 秒 GUI 等待或长验收中途重启，截断证据。 |
| `asterinas.mmc_write_partition2` | **必须出现** | 缺它时 MMC 以只读注册（`kernel/comps/mmc/src/block.rs:93`），p2 根文件系统写测试必失败。 |
| `console=tty0` | 必须是**第一个** `console=` | Asterinas 只用第一个 `console=` 作为 `/dev/console`；显示链要求它落到 HDMI（`tools/riscv/README.md` 的 firmware framebuffer 小节）。 |
| `console=ttyS0` | 作为第二个 console | 保留串口 shell 与早期日志。 |
| `init=/init` | 必须 | Stage1。 |
| `-- --root-init=systemd` | 必须 | 进入 Debian systemd。 |
| `--debug-console=root` | **每日镜像禁止** | 会占住 ttyS0，`asterinas-console-getty` 出现 208/STDIN / start-limit-hit（`tools/riscv/debian/rootfs/build_rootfs.sh:1773-1778`）。需要 root 就用 isolated-root 或 `sudo`。 |
| `asterinas.net=eic7700-rj45,<ip>/<prefix>,<gw>` | 网络判据必须 | 运行时不支持 `ip addr add`，地址只能由启动参数给。没有它 `ip -4 addr` 必然为空。 |
| `asterinas.neighbor=eic7700-rj45,<gw-ip>,<gw-mac>` | 网络判据建议 | 板上 ARP 路径不可靠；固定网关 MAC 才能稳定出网。MAC 用主机 `ip neigh` 查，按现场填写。 |
| `loglevel=info` | 建议 | `USB boot ... registered` 是 `info!`，`loglevel=off/warn` 下串口看不到；帧缓冲是 `early_println!`，不受影响。 |

启动后第一件事是读 `/proc/cmdline`，**一条命令判定**：

```sh
tr '\0' ' ' </proc/cmdline; echo
```

通过条件（四项同时满足）：

1. 不含 `asterinas.reboot_after=`；
2. 含 `asterinas.mmc_write_partition2`；
3. 第一个 `console=` 是 `tty0`；
4. 含 `-- --root-init=systemd`。

如果条目里带了 `asterinas.reboot_after`：

- 不要继续验收，它的结果无效。
- 在 U-Boot 里覆盖本次 bootargs（只改 RAM，不 `saveenv`）：

```text
setenv bootargs "console=tty0 console=ttyS0 loglevel=info asterinas.klog_capture=info init=/init asterinas.mmc_write_partition2 -- --root-init=systemd"
fdt set /chosen bootargs "$bootargs"
booti <kernel-addr> <initrd-addr>:${initrd_size} <dtb-addr>
```

其中 `${initrd_size}` 来自加载 Stage1 后的 `filesize`（`setenv initrd_size ${filesize}`）；
kernel/stage1/DTB 的地址以现场 `ext4load` 的输出/既有启动脚本为准。

- **不要**用 `tools/riscv/megrez_board_session.py --bootargs` 做这件事：它的
  `validate_writable_recovery`（`tools/riscv/megrez_board_session.py:963-1009`）在出现
  `asterinas.mmc_write_partition2` 时强制要求恰好一个 `asterinas.reboot_after`，与本剧本
  的硬要求冲突。走每日菜单或手工 `booti`。
- 如果本次要验网络，把 `asterinas.net=eic7700-rj45,<ip>/<prefix>,<gw>` 和
  `asterinas.neighbor=eic7700-rj45,<gw-ip>,<gw-mac>` 一并写进上面的 `bootargs`；
  不写就只能得到 `NET_ADDR`/`NET_ROUTE` 失败（属现场配置缺口，不是内核回归）。

### 1.4 启动与串口录制

主机侧（串口独占；`paced_serial_bridge.py` 会记录板上所有输出）：

```sh
ls -l /dev/serial/by-id/
id -nG | tr ' ' '\n' | grep -qx dialout
fuser -v /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_REPLACE-if00-port0 || true
python3 tools/riscv/paced_serial_bridge.py \
  /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_REPLACE-if00-port0 \
  /tmp/megrez-daily-acceptance.serial.log
```

bridge 用 `Ctrl-]` 退出；退出前先 `sync`，避免把未落盘的测试文件留在 p2。

上电后有约 30 s 的固件延迟，随后才出现 U-Boot；不要在这段时间断电。
U-Boot → 选 `asterinas-daily` → 等串口 shell。登录后先做三条预检：

```sh
id
tr '\0' ' ' </proc/cmdline; echo
systemctl is-system-running || true
systemctl --failed --no-legend
```

> `systemctl is-system-running` 当前很可能是 `degraded`（exit 1），这是已知缺口，
> 不是 P0 失败；P0 用 `local-fs.target` + `multi-user.target` 的 `is-active` 判定。

### 1.5 主机侧串口标记（显示与 USB 的硬证据）

```sh
grep -E 'ASTERINAS_FRAMEBUFFER_INIT status=(found|registered)' /tmp/megrez-daily-acceptance.serial.log
grep -c 'Starting DWC3 xHCI host' /tmp/megrez-daily-acceptance.serial.log
grep -E 'USB boot (keyboard|mouse) registered' /tmp/megrez-daily-acceptance.serial.log
grep -E 'ASTERINAS_GMAC_(SELECTED|READY)' /tmp/megrez-daily-acceptance.serial.log || true
```

通过条件：

- 帧缓冲：有 `status=found ... width=1920 height=1080 stride=7680` 且有
  `status=registered`（`kernel/comps/framebuffer/src/framebuffer.rs:113,169`）。
  出现 `status=missing` 表示 DTB 没有 framebuffer 节点；`status=map-failed` 表示地址/长度不对。
- USB：至少一条 `Starting DWC3 xHCI host`，且有
  `USB boot keyboard registered: ... name=usb_boot_keyboard` 与
  `USB boot mouse registered: ... name=usb_boot_mouse`
  （`kernel/comps/usb/src/arch/riscv/mod.rs:358,378,563`）。
  两个控制器各接一个设备时会是两条 host；同一 hub 下只计一条也正常。
- GMAC 标记是辅助信息；网络判据以 guest 的 `ip`/`getent`/`curl` 为准。

## 2. 最小验收脚本

### 2.1 `daily-acceptance.sh`

把下面内容存成 `/var/tmp/daily-acceptance.sh`（串口粘贴或从主机 `http.server` 下载），
用 `sh /var/tmp/daily-acceptance.sh` 运行。脚本不需要 root；如果当前是 root，
GUI/输入检查会自动降到 `asterinas` 用户执行。

```sh
#!/bin/sh
# Asterinas/Megrez Debian daily-use acceptance (POSIX sh).
# Emits ACC_<id>=ok|fail. Exit 0 only when every P0 check passed.

pass=0 p0_fail=0 p1_fail=0
ok()   { pass=$((pass+1));       echo "ACC_$1=ok${2:+ $2}"; }
bad0() { p0_fail=$((p0_fail+1)); echo "ACC_$1=fail${2:+ $2}"; }
bad1() { p1_fail=$((p1_fail+1)); echo "ACC_$1=fail${2:+ $2}"; }

# Pick the desktop user for X/input checks.
if [ "$(id -u)" = 0 ] && id asterinas >/dev/null 2>&1; then
    DUSER=asterinas
    asuser() { /usr/sbin/runuser -u "$DUSER" -- "$@"; }
else
    DUSER=$(id -un)
    asuser() { "$@"; }
fi
XAUTH="/home/$DUSER/.Xauthority"
XLOG="/home/$DUSER/Xorg.0.log"
tmp="/tmp/acc.$$"

# --- 0. boot contract -------------------------------------------------------
cmdline=$(tr '\0' ' ' </proc/cmdline 2>/dev/null || true)
case "$cmdline" in
    *asterinas.reboot_after=*) bad0 BOOT "reboot_after-present";;
    *)                         ok   BOOT "no-reboot-timer";;
esac
case "$cmdline" in
    *asterinas.mmc_write_partition2*) ok   BOOT_WRITE "write-gate-armed";;
    *)                                bad0 BOOT_WRITE "write-gate-missing";;
esac
case "$cmdline" in
    console=tty0*) ok BOOT_CONSOLE "tty0-first";;
    *)             bad0 BOOT_CONSOLE "tty0-not-first";;
esac
case "$cmdline" in
    *--root-init=systemd*) ok BOOT_SYSTEMD "root-init=systemd";;
    *)                     bad0 BOOT_SYSTEMD "root-init-missing";;
esac

# --- 1. systemd targets -----------------------------------------------------
pid1=$(cat /proc/1/comm 2>/dev/null || echo '?')
if [ "$pid1" = systemd ] &&
   systemctl is-active --quiet local-fs.target &&
   systemctl is-active --quiet multi-user.target; then
    ok SYSTEMD "pid1=systemd local-fs=active multi-user=active"
else
    bad0 SYSTEMD "pid1=$pid1 local-fs=$(systemctl is-active local-fs.target 2>/dev/null) multi-user=$(systemctl is-active multi-user.target 2>/dev/null)"
fi
state=$(systemctl is-system-running 2>/dev/null || true)
case "$state" in
    running) ok   SYSTEMD_RUNNING "state=running";;
    *)       bad1 SYSTEMD_RUNNING "state=${state:-unknown}";;
esac

# --- 2. root filesystem: 4 KiB write + fsync + read back --------------------
root_dev=$(stat -c %d / 2>/dev/null || echo '')
work=''
for d in "$HOME" /home/asterinas /var/tmp /root; do
    [ -d "$d" ] || continue
    [ "$(stat -c %d "$d" 2>/dev/null)" = "$root_dev" ] || continue
    work="$d/asterinas-acceptance-$$"; break
done
if [ -n "$work" ] && mkdir -p "$work" 2>/dev/null &&
   dd if=/dev/urandom of="$work/a.bin" bs=4096 count=1 conv=fsync status=none 2>/dev/null; then
    sync
    h1=$(sha256sum "$work/a.bin" | cut -d' ' -f1)
    dd if="$work/a.bin" of=/dev/null bs=4096 status=none 2>/dev/null
    h2=$(sha256sum "$work/a.bin" | cut -d' ' -f1)
    size=$(stat -c %s "$work/a.bin" 2>/dev/null || echo 0)
    if [ "$size" = 4096 ] && [ -n "$h1" ] && [ "$h1" = "$h2" ]; then
        ok FS "bytes=4096 path=$work"
    else
        bad0 FS "bytes=$size hash_match=$([ -n "$h1" ] && [ "$h1" = "$h2" ] && echo yes || echo no)"
    fi
else
    bad0 FS "cannot-write path=${work:-none} rootdev=$root_dev"
fi

# --- 3. /proc key fields ----------------------------------------------------
if [ -r /proc/self/comm ] && [ -n "$(cat /proc/self/comm 2>/dev/null)" ] &&
   [ -r /proc/mounts ] && grep -qE '[[:space:]]/[[:space:]]' /proc/mounts &&
   [ "$(cat /proc/1/comm 2>/dev/null)" = systemd ]; then
    ok PROC "self/comm mounts 1/comm"
else
    bad0 PROC "missing-or-empty"
fi

# --- 4. network -------------------------------------------------------------
addr=$(ip -4 -o addr show 2>/dev/null | awk '$2!="lo"{print $2"="$4; exit}')
[ -n "$addr" ] && ok NET_ADDR "$addr" || bad0 NET_ADDR "no-ipv4"
gw=$(ip route 2>/dev/null | awk '/^default/{print $3; exit}')
[ -n "$gw" ] && ok NET_ROUTE "gw=$gw" || bad0 NET_ROUTE "no-default"
if timeout 10 getent hosts deb.debian.org >/dev/null 2>&1; then
    ok NET_DNS "$(timeout 10 getent hosts deb.debian.org | awk 'NR==1{print $1}')"
else
    bad1 NET_DNS "getent-failed-or-timeout"
fi
code=$(curl -sS -L -o /dev/null -w '%{http_code}' --max-time 10 \
       https://deb.debian.org/debian/dists/trixie/Release 2>/dev/null || true)
case "$code" in
    200) ok   NET_HTTP "code=200";;
    *)   bad1 NET_HTTP "code=${code:-none}";;
esac

# --- 5. input ---------------------------------------------------------------
nodes=$(asuser /usr/lib/asterinas/desktop-input-identity 2>/dev/null || true)
if [ -n "$nodes" ]; then
    set -- $nodes
    if [ -c "$1" ] && [ -c "$2" ] && [ "$1" != "$2" ]; then
        ok INPUT "kbd=$1 ptr=$2"
    else
        bad0 INPUT "nodes=$nodes"
    fi
else
    bad0 INPUT "identity-resolver-failed"
fi
if grep -q 'Adding extended input device.*Asterinas keyboard' "$XLOG" 2>/dev/null &&
   grep -q 'Adding extended input device.*Asterinas pointer' "$XLOG" 2>/dev/null; then
    ok INPUT_XORG "keyboard+pointer"
else
    bad0 INPUT_XORG "xorg-log-missing"
fi

# --- 6. display -------------------------------------------------------------
[ -c /dev/fb0 ] && ok DISPLAY_FB "fb0=char" || bad0 DISPLAY_FB "fb0-absent"
if asuser env DISPLAY=:0 XAUTHORITY="$XAUTH" xwininfo -root >"$tmp" 2>/dev/null &&
   grep -q 'Width: 1920' "$tmp" && grep -q 'Height: 1080' "$tmp"; then
    ok DISPLAY_X "1920x1080"
else
    bad0 DISPLAY_X "$(tr '\n' ' ' <"$tmp" 2>/dev/null | cut -c1-120)"
fi

# --- 7. GUI app stays alive >10 s -------------------------------------------
gui_pid=''
if command -v xterm >/dev/null 2>&1; then
    asuser env DISPLAY=:0 XAUTHORITY="$XAUTH" xterm -title ACCEPTANCE -e sleep 30 >/dev/null 2>&1 &
    gui_pid=$!
elif command -v xmessage >/dev/null 2>&1; then
    asuser env DISPLAY=:0 XAUTHORITY="$XAUTH" xmessage -title ACCEPTANCE -timeout 30 ACCEPTANCE >/dev/null 2>&1 &
    gui_pid=$!
else
    bad0 GUI "no-xterm-or-xmessage"
fi
if [ -n "$gui_pid" ]; then
    sleep 10
    if kill -0 "$gui_pid" 2>/dev/null; then
        if asuser env DISPLAY=:0 XAUTHORITY="$XAUTH" xwininfo -root -tree 2>/dev/null | grep -q 'ACCEPTANCE'; then
            ok GUI "pid=$gui_pid alive=10s window=found"
        else
            bad0 GUI "pid=$gui_pid alive=10s window=missing"
        fi
    else
        bad0 GUI "pid=$gui_pid exited-before-10s"
    fi
    kill "$gui_pid" 2>/dev/null || true
    wait "$gui_pid" 2>/dev/null || true
    pkill -f 'xterm -title ACCEPTANCE' 2>/dev/null || true
    pkill -f 'xmessage -title ACCEPTANCE' 2>/dev/null || true
fi
rm -f "$tmp"

# --- summary ----------------------------------------------------------------
p0=pass; [ "$p0_fail" -eq 0 ] || p0=fail
p1=pass; [ "$p1_fail" -eq 0 ] || p1=fail
echo "ACC_SUMMARY p0=$p0 p1=$p1 ok=$pass p0_fail=$p0_fail p1_fail=$p1_fail"
[ "$p0_fail" -eq 0 ]
```

### 2.2 逐项命令与通过条件（每条判据一条命令）

| ID | 判据 | 一条命令（guest，除注明 host） | 通过条件 |
|---|---|---|---|
| BOOT | 无自毁计时器 | `grep -c 'asterinas.reboot_after=' /proc/cmdline` | 输出 `0` |
| BOOT_WRITE | p2 写能力已开 | `grep -c 'asterinas.mmc_write_partition2' /proc/cmdline` | 输出 `1` |
| BOOT_CONSOLE | 显示 console 优先 | `awk '{print $1}' /proc/cmdline` | 输出以 `console=tty0` 开头 |
| BOOT_SYSTEMD | 进入 systemd | `grep -c -- '--root-init=systemd' /proc/cmdline` | 输出 `1` |
| SYSTEMD | local-fs + multi-user | `systemctl is-active --quiet local-fs.target && systemctl is-active --quiet multi-user.target; echo $?` | `0` |
| SYSTEMD_RUNNING | 严格 running（P1） | `systemctl is-system-running` | `running`（当前预期 `degraded`，已知失败） |
| FS | 4 KiB 写+sync+读回 | 见下方 FS 单行命令 | 退出 `0`，size=4096，前后 hash 相同 |
| PROC | `/proc` 三字段 | `test -r /proc/self/comm && test -s /proc/self/comm && test -r /proc/mounts && [ "$(cat /proc/1/comm)" = systemd ]; echo $?` | `0` |
| NET_ADDR | 有线 IPv4 | `ip -4 -o addr show \| awk '$2!="lo"{print $2"="$4; exit}'` | 非空，例如 `eth0=10.100.19.200/21` |
| NET_ROUTE | 默认路由 | `ip route \| awk '/^default/{print $3; exit}'` | 非空，例如 `10.100.16.1` |
| NET_DNS | DNS（P1） | `timeout 10 getent hosts deb.debian.org` | 退出 `0` 且至少一行 |
| NET_HTTP | 外网（P1） | `curl -sS -L -o /dev/null -w '%{http_code}' --max-time 10 https://deb.debian.org/debian/dists/trixie/Release` | `200` |
| INPUT | 键鼠 evdev 身份 | `/usr/lib/asterinas/desktop-input-identity` | 输出两个不同的 `/dev/input/eventN`，退出 `0` |
| INPUT_XORG | Xorg 实际接管 | `grep -q 'Adding extended input device.*Asterinas keyboard' /home/asterinas/Xorg.0.log && grep -q 'Adding extended input device.*Asterinas pointer' /home/asterinas/Xorg.0.log` | 两条 `grep` 都退出 `0` |
| DISPLAY_FB | 帧缓冲节点 | `test -c /dev/fb0; echo $?` | `0` |
| DISPLAY_X | X 根窗口 1920×1080 | `DISPLAY=:0 XAUTHORITY=/home/asterinas/.Xauthority xwininfo -root` | `Width: 1920`、`Height: 1080` |
| DISPLAY_LOG | 内核帧缓冲初始化（host） | `grep -q 'ASTERINAS_FRAMEBUFFER_INIT status=found .*width=1920 height=1080' /tmp/megrez-daily-acceptance.serial.log && grep -q 'ASTERINAS_FRAMEBUFFER_INIT status=registered' /tmp/megrez-daily-acceptance.serial.log` | 两条 `grep` 都退出 `0` |
| GUI | GUI 应用 >10 s | 见下方 GUI 单行命令 | 进程 10 s 后仍存活，且 `xwininfo -root -tree` 有 `ACCEPTANCE` |
| INPUT_LOG | USB 注册（host） | `grep -q 'USB boot keyboard registered' /tmp/megrez-daily-acceptance.serial.log && grep -q 'USB boot mouse registered' /tmp/megrez-daily-acceptance.serial.log` | 两条 `grep` 都退出 `0` |

FS 单行命令（在 root 文件系统上写，不在 tmpfs）：

```sh
d=/var/tmp/acc.$$; mkdir -p "$d" && dd if=/dev/urandom of="$d/a" bs=4096 count=1 conv=fsync status=none && sync && h1=$(sha256sum "$d/a"|cut -d' ' -f1) && dd if="$d/a" of=/dev/null bs=4096 status=none && h2=$(sha256sum "$d/a"|cut -d' ' -f1) && [ -n "$h1" ] && [ "$h1" = "$h2" ] && [ "$(stat -c%s "$d/a")" = 4096 ]; echo "FS_RC=$?"
```

> 这条判据验证的是“写入 + fsync + 逻辑读回”，不是掉电持久性。ext2 无日志，
> `asterinas.reboot_after` 也不会替你 sync；掉电持久性属于另一条门禁。
> 如果 `/var/tmp` 被挂成 tmpfs，用上面的脚本（它会挑一个与 `/` 同设备的目录），
> 不要用这条简化单行。

GUI 单行命令：

```sh
DISPLAY=:0 XAUTHORITY=/home/asterinas/.Xauthority xterm -title ACCEPTANCE -e sleep 30 & p=$!; sleep 10; kill -0 $p && DISPLAY=:0 XAUTHORITY=/home/asterinas/.Xauthority xwininfo -root -tree | grep -q ACCEPTANCE; echo "GUI_RC=$?"; kill $p 2>/dev/null
```

如果当前 shell 是 root，把 `xterm` 和 `xwininfo` 用
`runuser -u asterinas -- env DISPLAY=:0 XAUTHORITY=/home/asterinas/.Xauthority ...` 包一层；
脚本已经这样处理。

## 3. 判定表（✅/❌ + 最小排查）

“当前预期”是 2026-10-28 镜像与本文撰写时的判断；`⚠️` 表示**需实测确认**，不是通过。

| # | 检查 | 层级 | 当前预期 | 失败时的最小排查（逐条可执行） |
|---|---|---|---|---|
| 1 | 启动参数无 `reboot_after` | P0 | ✅ | `cat /proc/cmdline`；回到 U-Boot `fdt print /chosen bootargs`；确认没选到 Probe/Basic 条目 |
| 2 | `mmc_write_partition2` 已开 | P0 | ✅ | `cat /proc/cmdline`；`findmnt -n -o SOURCE,OPTIONS /`；`sudo dmesg \| grep -E -e 'mmcblk0' -e 'partition-2'` |
| 3 | `console=tty0` 第一 | P0 | ✅ | `cat /proc/cmdline`；串口 `grep ASTERINAS_FRAMEBUFFER_INIT`；显示器是否有 U-Boot/内核画面 |
| 4 | systemd local-fs + multi-user | P0 | ⚠️ 需实测确认 | `systemctl status local-fs.target multi-user.target --no-pager`；`systemctl list-jobs`；`journalctl -b -u local-fs.target --no-pager -n 30` |
| 5 | `is-system-running` | P1 | ❌ 已知 `degraded` | `systemctl --failed --no-legend`；`systemctl status <unit> --no-pager`；`journalctl -b -u <unit> --no-pager -n 40` |
| 6 | 根文件系统 4 KiB 写回 | P0 | ✅ | `findmnt -n -o SOURCE,FSTYPE,OPTIONS /`；`cat /proc/cmdline`；`sudo dmesg \| grep -iE -e 'mmc' -e 'ext2' -e 'read-only'`；`ls -ld /var/tmp` |
| 7 | `/proc` 三字段 | P0 | ✅ | `ls -l /proc/self/comm /proc/mounts /proc/1/comm`；`cat /proc/1/comm`；`grep ' on /proc ' /proc/mounts` |
| 8 | 有线 IPv4 | P0 | ⚠️ 取决于 `asterinas.net` | `ip -br link`；`ip -4 addr`；`grep -o 'asterinas.net=[^ ]*' /proc/cmdline`；`sudo dmesg \| grep -i gmac` |
| 9 | 默认路由 | P0 | ⚠️ 取决于 `asterinas.net` | `ip route`；`cat /proc/net/route`；`grep -o 'asterinas.neighbor=[^ ]*' /proc/cmdline` |
| 10 | DNS | P1 | ❌ 已知（镜像 `/etc/resolv.conf` 是 `10.0.2.3`） | `cat /etc/resolv.conf`；`timeout 10 getent ahostsv4 deb.debian.org`；`systemctl is-active asterinas-dns-shim`；`grep -o 'ASTERINAS_DESKTOP_DNS[^ ]*' /proc/cmdline` |
| 11 | 外网 HTTP | P1 | ❌ 已知（网关/代理未证明） | `curl -v --max-time 5 https://deb.debian.org/`；`curl -v --max-time 5 http://10.100.16.1/`；`ip route`；`date -u`；`curl -k` 区分 TLS 与路由 |
| 12 | USB 键鼠注册 | P0 | ✅（HID 修复已上板） | 串口 `grep -E -e 'USB boot keyboard registered' -e 'USB boot mouse registered' -e 'Selected DWC3 USB host'`；`ls -l /dev/input/event*`；`sudo dmesg \| grep -iE -e xhci -e hid -e usb -e SET_IDLE -e Babble` |
| 13 | Xorg 接管键鼠 | P0 | ✅ | `grep 'Adding extended input device.*Asterinas' /home/asterinas/Xorg.0.log`；`grep -A4 'Identifier "Asterinas pointer"' /etc/asterinas/display-providers/*/xorg.conf.d/20-asterinas.conf` |
| 14 | `/dev/fb0` | P0 | ✅ | `ls -l /dev/fb0`；串口 `grep ASTERINAS_FRAMEBUFFER_INIT`；`grep -i fbdev /home/asterinas/Xorg.0.log` |
| 15 | 1920×1080 | P0 | ✅ | `xwininfo -root`；`grep -F 'FBDEV(0)' /home/asterinas/Xorg.0.log`；串口 `grep width=1920` |
| 16 | GUI 应用 >10 s | P0 | ⚠️ 需实测确认 | `pgrep -a xterm`；`xwininfo -root -tree`；`systemctl status asterinas-desktop-m5.service --no-pager`；`tail -40 /home/asterinas/desktop-m5-session.log` |

判读：

- #4/#8/#9/#16 若失败，先按“排查”列定位；它们都是 P0，不能因为“以前通过过”而跳过。
- #5/#10/#11 是当前明确的 P1 缺口；失败时写进第 4 节，不要改写成 `ok`。
- #12 的串口标记依赖 `loglevel=info`；若启动参数是 `loglevel=off`，改用
  `desktop-input-identity` + Xorg 日志判定，并在报告里注明“串口标记不可见”。

## 4. 已知缺口（明确失败，不伪装通过）

1. **`systemctl is-system-running` 不是 `running`。**
   2026-10-27 基线为 `degraded`（4 个 failed unit：`asterinas-console-getty`、
   `asterinas-desktop-m5-network`、`asterinas-dns-shim`、`systemd-sysctl`；
   见 `docs/porting/evidence/2026-10-27-board-ux-baseline.md` §A2）。
   P1 镜像已把 sysctl key 注释掉、让网络/DNS unit 快速失败，预期只剩
   `asterinas-desktop-m5-network` + `systemd-random-seed`（PR #228 分支的
   `2026-10-27-daily-use-p1.md` §3）。**本剧本以 `local-fs.target`/`multi-user.target`
   的 `is-active` 作为 P0，不把严格 `running` 当作通过。**

2. **DNS 与外网未证明。**
   2026-10-28 安装的镜像 `/etc/resolv.conf` 仍是构建容器的 `nameserver 10.0.2.3`；
   网关 `10.100.16.1` 在宿主上也不可达，guest 直连外网未跑通；host proxy bridge
   的 guest 侧也未证明（`docs/porting/board-image-current.md` §6，commit `eeba8dc89`）。
   因此 `getent hosts deb.debian.org` 和 `curl ...` 当前标 **❌ 已知失败**。
   修好后的判据不变：`getent` 退出 0 + `curl` 返回 200。

3. **`/proc/bus/input/devices` 不存在。**
   Asterinas 不提供这个 procfs 文件；等价路径是 `/dev/input/event*` + `EVIOCGID/
   EVIOCGNAME/EVIOCGPHYS`（`tools/riscv/debian/rootfs/desktop_input_identity.py`），
   以及 Xorg 日志的 `Adding extended input device.*Asterinas keyboard/pointer`。
   **不要**把 `/proc/bus/input/devices` 写成判据。

4. **串口普通用户 getty 在旧镜像上失败。**
   旧镜像 `asterinas-console-getty` 因 systemd `TIOCSCTTY(1)` 的 steal 语义缺失，
   报 208/STDIN + start-limit-hit（`docs/porting/evidence/2026-10-27-board-ux-fix.md`
   §B2）。每日镜像通过“不传 `--debug-console=root`”绕开（`build_rootfs.sh:1773`），
   但新镜像上 `asterinas@` autologin 是否稳定**需实测确认**；本次验收以实际串口
   shell 为准（root debug 或 asterinas 均可）。

5. **显示只有 1920×1080 fbdev，不是原生 HDMI/DRM。**
   当前是 U-Boot 交接的 firmware framebuffer（`/dev/fb0`，base `0xfd800000`，
   stride 7680）。原生 EIC7700 显示控制器、GPU 加速、显示器原生 2560×1600
   都不在本剧本范围；`xwininfo` 的 1920×1080 是 guest 的真实分辨率。

6. **`systemd-random-seed` 等兼容性 unit。**
   旧镜像的 sysctl 两条 key 已在镜像侧注释；`systemd-random-seed` 仍未在 P1 范围。
   这些 unit 影响 `is-system-running`，不影响 `local-fs.target`/桌面启动，按 P1 记录。

7. **没有自动恢复定时器。**
   本剧本明确禁止 `asterinas.reboot_after`，因此不会再出现“到点回 U-Boot”的自动兜底。
   `reboot_after` 本身也不 sync 文件系统
   （`book/src/kernel/linux-compatibility/kernel-parameters.md:259-266`），
   不适合有写操作的验收。**验收必须有人值守；挂起用物理 reset。**

8. **工具链边界：`megrez_board_session.py` 不能直接跑本配置。**
   它在出现 `asterinas.mmc_write_partition2` 时强制要求一个 `asterinas.reboot_after`
   （`tools/riscv/megrez_board_session.py:963-1009`）。这是工具的安全策略，不是内核缺口；
   本剧本用每日菜单或手工 `booti` 绕开。

9. **本剧本不含 apt / Firefox 页面渲染 / 视频性能。**
   这些分别由 Debian APT 域、`tools/riscv/debian/rootfs` 的浏览器 gate 和性能线负责。
   本剧本的“桌面”只要求一个 GUI 应用起来并存活 >10 s。
   旧基线里 `xterm -e true` 曾在 5 s 内不退出（`2026-10-27-board-ux-baseline.md` §A5）；
   若 GUI 项失败但 Xorg/`xwininfo` 正常，先用 `xmessage` 交叉验证，区分 xterm pty
   问题与桌面问题。

## 5. 证据记录模板

每次验收至少保留：

```sh
# guest（和脚本输出一起保存）
date -u; uname -a; id
tr '\0' ' ' </proc/cmdline; echo
findmnt -n -o SOURCE,FSTYPE,OPTIONS /
systemctl is-system-running || true
systemctl --failed --no-legend
ip -4 addr; ip route; cat /etc/resolv.conf
sh /var/tmp/daily-acceptance.sh 2>&1 | tee /var/tmp/daily-acceptance.log
```

```sh
# host
sha256sum kernel-daily.Image stage1-daily.cpio megrez-daily.dtb   # 按实际文件名替换
grep -E 'ASTERINAS_FRAMEBUFFER_INIT|USB boot (keyboard|mouse) registered|Starting DWC3 xHCI host' \
  /tmp/megrez-daily-acceptance.serial.log > /tmp/daily-acceptance-markers.log
ls -l /tmp/megrez-daily-acceptance.serial.log /tmp/daily-acceptance-markers.log
```

报告里写清楚：镜像 SHA-256、kernel/stage1/DTB 的 SHA-256/CRC32、串口日志路径、
`ACC_SUMMARY` 原文、P1 已知失败的当前现象。没有这些身份，结论不可复现。

## 6. 平台边界

- 本文是 **Megrez RISC-V 板端**剧本；x86 Debian 的 L1–L6/日用矩阵用
  `tools/x86/debian/acceptance_gate.sh`（见 `docs/porting/2026-10-25-debian-usability-report.md`
  所在分支），不要把两边的通过互相替代。
- QEMU 的 virtio 键鼠/显示门禁（`tools/riscv/xhci/input_gate.py`、
  `tools/riscv/drm/` 等）证明的是软件链路，不证明板端 xHCI/HID、HDMI 或 SD/eMMC；
  本剧本只认板端串口与 HDMI 的证据。
- 本剧本只写运行命令，不改内核、不改 ostd 抽象；如需修内核，另开分支与 PR。
