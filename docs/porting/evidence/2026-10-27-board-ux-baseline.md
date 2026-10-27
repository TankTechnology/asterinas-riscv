# 2026-10-27 Megrez Asterinas Debian 日用 UX 基线（A）

- Board: Milk-V Megrez / Asterinas 5.13.0 / Debian 13 trixie
- Guest prompt: `root@asterinas-debug:/#`（调试 root shell；普通 `asterinas@` getty 未起来）
- Root: `/dev/mmcblk0p2 ext2 (rw,relatime)`
- Uptime at first sample: `1765.60 s`；后续 `1874.17 s`
- 串口 115200 单写者；所有命令拆短，逐字符 ~18 ms 慢发。
- 原始日志：`/tmp/board_ux_a.log`（本轮未跟踪本地文件）。
- 状态：**A 基线完成；B 修复前**。不刷 eMMC、不改 boot 分区、未 push。

## A0. 汇总判定

| 项 | 结果 | 判定 |
| --- | --- | --- |
| `systemctl is-system-running` | `degraded`（exit 1） | **fail**（目标 running） |
| failed 单元数 | **4** | **fail**（目标 0） |
| boot → graphical.target | kernel 9.419 s + userspace 74.197 s；graphical 42.090 s in userspace（monotonic 51.5 s） | **partial**（无 U-Boot 菜单时刻，见 A1） |
| desktop 服务进入 active | monotonic **83.597 s** | **fail/慢**（用户要的桌面就绪明显晚于 graphical） |
| 普通 `asterinas@` 登录 | console-getty **failed**（start-limit-hit, agetty 208/STDIN） | **fail** |
| fsync 5×4 KiB | **0.694 s**（另一次 0.901 s） | **fail/慢**（PR #208 目标） |
| `apt-get -s install sl` | `E: Unable to locate package sl`（exit 100） | **fail**（包列表不可解析） |
| X root window | 1920×1080、Depth 24、IsViewable | **ok** |
| `firefox` 命令 | `firefox: No such file`（exit 2）；`firefox-esr` 未在本轮验证 | **fail/未知**（需 B 再查实际二进制） |
| `xterm -e true` | `timeout 5` → **124**（未在 5 s 内退出） | **fail/未知** |
| `pcmanfm` 进程 | 无 | **未知**（用户说在，当前未看到） |
| `reboot` 路径 | `/usr/sbin/reboot` → `../bin/systemctl`；root 有；`su asterinas` 也能看到 | **未知**（未实际按普通用户执行；P1 代码未在当前镜像） |
| 网络 | `eth0` UP 但**无 IPv4、无路由**；`ping`/`getent` 无返回 | **fail** |
| 根文件系统 | `/dev/mmcblk0p2 ext2 (rw,relatime)` | **ok** |

## A1. 开机到桌面（guest 侧证据）

```text
# systemd-analyze
Startup finished in 9.419s (kernel) + 1min 14.197s (userspace) = 1min 23.616s
graphical.target reached after 42.090s in userspace.

# systemctl show -p ActiveEnterTimestampMonotonic graphical.target
ActiveEnterTimestampMonotonic=51509283

# systemctl show -p ActiveEnterTimestampMonotonic asterinas-desktop-m5.service
ActiveEnterTimestampMonotonic=83597096

# systemctl show -p ActiveEnterTimestampMonotonic asterinas-console-getty.service
ActiveEnterTimestampMonotonic=97934123   （但该 unit failed）
```

判定：guest 侧 **graphical.target ≈ 51.5 s**（9.419 + 42.090），
desktop 服务 active ≈ **83.6 s**；`asterinas@` 登录 **失败**。
**U-Boot 菜单选择时刻没有 guest 时间戳，本轮不能把“菜单→guest”合成一个精确秒数**；
这是 A 的未知项，不是估算项。用户要求的“菜单选择→asterinas@/图形目标”目前只能给出
guest 侧数字，`asterinas@` 为 fail。

## A2. failed units（4）

```text
# systemctl --failed --no-legend
● asterinas-console-getty.service      loaded failed failed Asterinas serial console (autologin)
● asterinas-desktop-m5-network.service loaded failed failed Asterinas Debian M5 wired-network evidence
● asterinas-dns-shim.service           loaded failed failed Asterinas guest DNS tunnel to the build host
● systemd-sysctl.service               loaded failed failed Apply Kernel Variables
# systemctl --failed --no-legend | wc -l
4
```

### systemd-sysctl

```text
Active: failed (Result: exit-code) since ... 19:38:31
Process: 30 ExecStart=/usr/lib/systemd/systemd-sysctl (code=exited, status=1/FAILURE)
```

未在 A 的 status 输出中拿到具体不被支持的 key；B 需要 `journalctl -u systemd-sysctl`
或 `systemd-sysctl` 的 stderr 定位。判定 **fail，原因未定位**。

### asterinas-console-getty

```text
Active: failed (Result: start-limit-hit) since ... 19:39:33
Process: 140 ExecStart=/sbin/agetty --autologin asterinas --noclear \
  --keep-baud 115200,...,9600 -l /usr/local/sbin/asterinas-console-login ttyS0 linux
  (code=exited, status=208/STDIN)
```

agetty 反复退出（restart counter 13）→ start-limit-hit。判定 **fail**；
B 需对照 ttyS0/控制终端/STDIN 的同类问题。

### asterinas-dns-shim

```text
Active: failed (Result: exit-code) since ... 19:38:52
Process: 58 ExecStart=/usr/lib/asterinas/megrez-dns-shim (code=exited, status=3)
megrez-dns-shim[58]: dns shim unconfigured: ASTERINAS_DESKTOP_DNS_HOST is unset or invalid
```

判定 **fail**：网络未配置的 bootargs 下 shim 以 3 失败，当前镜像没有 P1 的
oneshot/inactive 处理。

### asterinas-desktop-m5-network

```text
Active: failed (Result: exit-code) since ... 19:38:41
Process: 60 ExecStart=/usr/lib/asterinas/desktop-m5-network-evidence
  (code=exited, status=1/FAILURE)
Drop-In: .../browser-web.conf
```

启动到失败约 7 s，但 unit 仍记 failed；当前镜像的 drop-in 仍是旧超时。
判定 **fail**；B/新镜像应 fast-fail 且不拖 boot。

## A3. fsync

```text
# cat /tmp/fs.sh
#!/bin/sh
for i in 1 2 3 4 5; do
dd if=/dev/zero of=/tmp/a$i bs=4k count=1 conv=fsync status=none
done

# time sh /tmp/fs.sh
real    0m0.694s
user    0m0.039s
sys     0m0.657s
```

另一次等价 `/tmp/z$i` 运行：`real 0m0.901s`。5 次 4 KiB fsync ≈ **0.69–0.90 s**
（≈139–180 ms/次），相对 Linux 的 ~ms 级明显慢。判定 **fail**；PR #208 的 ext2
dirty-range/skip-clean-inode 修复是候选，但尚未合并/刷入。

## A4. apt 软件安装解析

```text
# apt-get -s install sl
Reading package lists... Done
Building dependency tree... Done
Reading state information... Done
E: Unable to locate package sl
（exit 100）
```

判定 **fail**：包列表里没有 `sl`，或列表未更新；不是网络拉包失败，而是当前 apt
索引无法解析该包。B 需检查 `/etc/apt/sources.list*` 与 `apt-get update` 能力。

## A5. 图形

```text
# DISPLAY=:0 xwininfo -root
xwininfo: Window id: 0x42 (the root window)
  Width: 1920
  Height: 1080
  Depth: 24
  Map State: IsViewable
（exit 0）
```

判定 **ok**：X 根窗口存在，1920×1080。

```text
# firefox --version
/bin/sh: 0: cannot open firefox: No such file
（exit 2）

# pgrep -a firefox | head -3
（无进程）

# pgrep -a pcmanfm | head -3
（无进程）

# pgrep -a xterm | head -3
（无进程）

# DISPLAY=:0 timeout 5 xterm -e true
（timeout 返回 124，未在 5 s 内退出）
```

判定：`firefox` **命令不存在/fail**；`firefox-esr` 实际路径本轮 A2 串口未取到，
记为 **未知**；`xterm` **未知/fail（5 s 未退出）**；`pcmanfm` 进程 **未知**。
用户说应用“都在”，但当前调试 boot 没有看到它们，需 B 重新核实实际二进制与启动方式。

## A6. 重启

```text
# command -v reboot
/usr/sbin/reboot
# ls -l /sbin/reboot /usr/sbin/reboot
lrwxrwxrwx ... /sbin/reboot -> ../bin/systemctl
lrwxrwxrwx ... /usr/sbin/reboot -> ../bin/systemctl
# id
uid=0(root) ...
# su -s /bin/sh asterinas -c 'command -v reboot; ls -l /sbin/reboot'
/usr/sbin/reboot
lrwxrwxrwx ... /sbin/reboot -> ../bin/systemctl
```

判定 **未知**：路径存在，root 可直接用；普通 `asterinas` 用户的实际 reboot 未执行
（本轮不做破坏性验证）。当前镜像没有 P1 的 sudo/polkit；A 只确认“命令可见”。

## A7. 网络

```text
# ip addr
1: lo ... inet 127.0.0.1/8
2: eth0 <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1514
    link/ether 00:48:54:71:00:48 ...
（无 inet 行）
# ip route
（空）
# ping -c1 -W1 10.100.16.1
（无输出/未返回）
# getent hosts deb.debian.org
（无输出/未返回）
```

判定 **fail**：eth0 有 link 但**没有 IPv4 地址、没有默认路由**；DNS 查询和网关 ping
都没有结果。B 的网络/ DNS 快速失败与用户体验高度相关。

## A8. 结论与 B 优先级

用户第一眼会撞到的顺序：**没有网络/IP → 没有可登录的普通 console（getty failed）
→ 不能装包（sl 不存在）→ 桌面/应用不确定 → reboot 不确定 → boot 83 s/desktop 83.6 s
→ fsync 慢**。

B 按此顺序处理：
1. `systemd-sysctl` 的 unsupported key（先定位再决定内核补或镜像删）；
2. `asterinas-console-getty` 的 ttyS0/STDIN start-limit（与今天 thinkpad 的
   pty/控制终端同类）；
3. `asterinas-dns-shim` / `m5-network` fast-fail（当前镜像仍无限/失败状态）；
4. `reboot` 普通用户路径（P1 code 未进当前镜像，先记录/等新镜像）；
5. fsync（PR #208 需要按新规则 base=main 重做后再验）。

## 复现与证据

- 原始 A 日志：`/tmp/board_ux_a.log`（本机；未跟踪）。
- A2 的 `journalctl`/二进制补充检查在本轮串口上未返回（后台任务只看到命令回显，
  没有 marker），故对应项标 **未知**，没有用记忆补。
- 所有命令均为短行/分步；串口单写者；未 reboot、未刷 eMMC、未改 boot 分区。
