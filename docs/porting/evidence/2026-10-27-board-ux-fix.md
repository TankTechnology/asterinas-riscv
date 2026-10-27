# 2026-10-27 Megrez Asterinas Debian 日用 UX 修复（B）

- Board: Milk-V Megrez / Asterinas 5.13.0 / Debian 13 trixie
- Guest prompt: `root@asterinas-debug:/#`（调试 root shell）
- Root: `/dev/mmcblk0p2 ext2 (rw,relatime)`（旧镜像；本轮没有刷机）
- 串口 `/dev/ttyUSB0` 115200 8N1，**单写者**；命令拆短、逐字符 ~18 ms 慢发。
- 本轮原始串口日志（本机未跟踪）：`/tmp/board_b2.log`、`/tmp/board_b3.log`、
  `/tmp/board_b4.log`、`/tmp/board_b5.log`；A 基线见
  `docs/porting/evidence/2026-10-27-board-ux-baseline.md`。
- 代码分支：`fix/board-ux-b`，基于 `origin/main` = `4d9dd7e9b`；PR base = `main`。
- 纪律：**未刷 eMMC、未改 boot 分区、未重启板子**；串口单写者。

## B0. 汇总判定

| 项 | 发现 | 判定 | 处置 |
| --- | --- | --- | --- |
| B① systemd-sysctl 失败 key | `kernel.pid_max`（存在但写入 `EOPNOTSUPP`）与 `kernel.core_pattern`（`/proc/sys` 中不存在） | **fail，根因确定** | 镜像侧删掉两条赋值（内核补 knob 无意义，见 B1） |
| B② console-getty 208/STDIN | systemd `acquire_terminal(FORCE)` 对 ttyS0 发 `TIOCSCTTY(1)` 得到 `EPERM`；手动 agetty 可登录 | **fail，根因确定** | **不是 PR #211 同一根因**；需内核补 `TIOCSCTTY` 的 steal 语义或改调试控制台占用，本轮只判定 |
| B③ m5-network / dns-shim | 板上旧镜像 m5 exit 1（~7 s）、dns exit 3 → failed；main 上两者仍是“失败/重启”语义 | **fail → 代码已修** | m5 45 s、fixture 缺失 exit 75 → inactive、不重启；dns 改为 `Restart=no` + `SuccessExitStatus=1 3`（不是 oneshot，理由见 B3） |
| 代码测试 | 焦点用例全过；全量 `test_debian_m5_network` 只剩 **4 个在 origin/main 上本来就失败**的用例 | **ok（有既有失败）** | 见 B3 测试明细 |

## B1. systemd-sysctl 到底哪个 key 失败

### 命令与真实输出

```text
# journalctl -u systemd-sysctl -b --no-pager | tail -25
Apr 13 19:38:31 62f003cbb76e systemd[1]: systemd-sysctl.service: Main process exited, code=exited, status=1/FAILURE
Apr 13 19:38:31 62f003cbb76e systemd[1]: systemd-sysctl.service: Failed with result 'exit-code'.
Apr 13 19:38:31 62f003cbb76e systemd[1]: Failed to start systemd-sysctl.service - Apply Kernel Variables.
```

journal 只有 exit 1，**没有 key 名**（这台镜像的 journal 没保留 stderr 细节）。

```text
# ls /usr/lib/sysctl.d /etc/sysctl.d
/etc/sysctl.d:
README.sysctl

/usr/lib/sysctl.d:
10-coredump-debian.conf  50-pid-max.conf
```

```text
# cat /usr/lib/sysctl.d/50-pid-max.conf
...
# Bump the numeric PID range to make PID collisions less likely.
# 2^22 and 2^15 is possible maximum of 64bit and 32bit kernels respectively.
kernel.pid_max = 4194304

# cat /usr/lib/sysctl.d/10-coredump-debian.conf
kernel.core_pattern=core
```

修正上轮的命令：`systemd-sysctl` 不在 PATH，且这个版本不支持 `--stderr`：

```text
# S=/usr/lib/systemd/systemd-sysctl; $S --stderr 2>&1 | head -10
/usr/lib/systemd/systemd-sysctl: unrecognized option '--stderr'
```

### 逐 key 验证内核支持

```text
# cat /proc/sys/kernel/pid_max
2147483647

# echo 4194304 > /proc/sys/kernel/pid_max; echo rc=$?; cat /proc/sys/kernel/pid_max
bash: echo: write error: Operation not supported
rc=1
2147483647
```

→ `kernel.pid_max` **节点存在但不可写**（`EOPNOTSUPP`），systemd-sysctl 写它就失败。

```text
# cat /proc/sys/kernel/core_pattern
cat: /proc/sys/kernel/core_pattern: No such file or directory

# echo core > /proc/sys/kernel/core_pattern; echo rc=$?
bash: /proc/sys/kernel/core_pattern: Operation not permitted
rc=1

# sysctl kernel.core_pattern; echo rc=$?
sysctl: cannot stat /proc/sys/kernel/core_pattern: No such file or directory
rc=1
```

→ `kernel.core_pattern` **在 Asterinas 中不存在**（没有 coredump 子系统）。

```text
# sysctl kernel.pid_max; echo rc=$?
rc=255
```

→ `sysctl(8)` 自身在这台 procfs 上读也失败（旁证，不是主证据）；
主证据是上面 `/proc/sys` 的直接读写。

### 判定与处置

**判定：fail，两个 key 都不被支持。** 这解释了 `systemd-sysctl.service`
每次启动都 exit 1，而不是某个 `net.*` key。

内核侧不补：

- `kernel.pid_max` 当前已经是 `2147483647`（最大值），补一个“可写”只会
  把值改小到 4194304，没有功能收益；
- `kernel.core_pattern` 需要完整的 coredump 支持，不是补一个 procfs 节点
  就能成立；镜像里也没有依赖它。

因此按用户规则走镜像侧：`build_rootfs.sh` 新增
`normalize_unsupported_sysctl_keys`，在 phase 7 对
`/usr/lib/sysctl.d` 和 `/etc/sysctl.d` 下的 `*.conf` 把这两条赋值注释掉
（文件保留，包布局不变）。**不是**空 `/etc/sysctl.d/99-drop.conf`——空
drop-in 不会阻止 systemd-sysctl 继续读 `/usr/lib` 的原始文件。

## B2. console-getty 失败与 PR #211 是否同根因

### systemd 侧证据

```text
# journalctl -u asterinas-console-getty -b --no-pager | tail -25
Apr 13 19:39:23 ... Scheduled restart job, restart counter is at 7.
Apr 13 19:39:24 ... (agetty)[122]: Failed to set up standard input: Operation not permitted
Apr 13 19:39:24 ... (agetty)[122]: Failed at step STDIN spawning /sbin/agetty: Operation not permitted
...
Apr 13 19:39:33 ... Start request repeated too quickly.
Apr 13 19:39:33 ... Failed with result 'start-limit-hit'.
```

unit 内容（板上）：

```text
ExecStart=-/sbin/agetty --autologin asterinas --noclear --keep-baud \
  115200,57600,38400,9600 -l /usr/local/sbin/asterinas-console-login ttyS0 linux
Restart=always
StandardInput=tty-force
TTYPath=/dev/ttyS0
```

208 = systemd 的 `EXIT_STDIN`；`setsid()` 失败会是另一个状态
（`EXIT_SETSID`），所以 **`setsid()` 已成功，失败发生在 `setup_input` 的
TTY 获取阶段**。

### 手动 agetty（同一 tty，手动跑）

```text
# agetty --autologin asterinas --noclear --keep-baud 115200 ttyS0

Debian GNU/Linux 13 62f003cbb76e ttyS0

62f003cbb76e login: asterinas (automatic login)
...
（回到 root@asterinas-debug:/#）
```

→ 手动 agetty **能起并自动登录**，说明 ttyS0 设备本身和 agetty 没问题；
差异在 systemd 的 `StandardInput=tty-force` 路径。

### 最小复现 TIOCSCTTY 的 EPERM

```text
# setsid -w true; echo rc=$?
rc=0

# setsid -w -c true; echo rc=$?
setsid: failed to set the controlling terminal: Operation not permitted
rc=1
```

`setsid -w -c` 会新建会话后对当前 tty（ttyS0）发 `TIOCSCTTY(0)`；它拿到
**EPERM**。这与 systemd 的失败是同一类调用，且不涉及 pty 或 xterm。

### 机制对照

systemd 的 `acquire_terminal(tty, ACQUIRE_TERMINAL_FORCE)`（tty-util.c）：

1. 打开 `/dev/ttyS0`（`O_RDWR|O_NOCTTY`）；
2. 调 `ioctl(fd, TIOCSCTTY, 1)`（FORCE 时 arg=1；`setsid -c` 用的是 arg=0）；
3. 只有 EPERM 会被 FORCE 直接传播 → `setup_input` 返回 EPERM → 208/STDIN。

Asterinas 侧：

- `SetControlTty` 对 `arg == 1` 只打印
  `stealing TTY from another session is not supported`，没有实现 steal；
- `JobControl::set_session` 在终端已有**存活会话**时返回 EPERM
  （`kernel/src/process/process/job_control.rs:52-65`）；
- 调试控制台 shell 进程（`ps` 中 `PID 69, PGID 69, SID 69`）的 stdin 是
  `/dev/ttyS0`（`/proc/69/fd/0 -> /dev/ttyS0`），其会话仍存活。

因此 `TIOCSCTTY(1)` 的“带 CAP_SYS_ADMIN 抢占已有会话终端”这条 Linux 语义
在 Asterinas 缺失，systemd 的 getty 永远拿不到 ttyS0；手动 agetty 不强制
ctty，所以看起来正常。

### 与 PR #211 的对照

PR #211（`fix/tty-pty-on-main`，base `main`）：

- 记录 `O_NOCTTY` 到 `StatusFlags`；
- 在 **devpts 从端 open 路径** 调 `acquire_control_on_open`，模拟 Linux
  `tty_open_proc_set_tty`，修复 xterm `setsid → open pty slave → /dev/tty`
  失败后反复重试；
- **不修改 `TIOCSCTTY` ioctl**，不改真实 tty（ttyS0）的 job control。

**判定：不是同一根因。** 同属“控制终端/会话”子系统，但 PR #211 修的是
pty 从端的 **open 时自动认领**；本板的失败是 ttyS0 上 **`TIOCSCTTY(1)`
抢占语义缺失**（systemd 在 exec 前强制把 ttyS0 变成 getty 的控制终端）。
PR #211 合入后，这个 208/STDIN 仍会存在。

后续修复选项（本轮不做，需单独内核改动）：

1. 在 `Terminal::set_control` 实现 `arg == 1` + `CAP_SYS_ADMIN` 的 steal：
   把终端从前一个会话摘下并挂到新会话（Linux `tiocsctty` 语义）；
2. 或让 Asterinas 调试控制台的 shell 不把 ttyS0 认作自己会话的控制终端，
   使 getty 可以正常认领。

## B3. 网络 unit 快速失败（代码已改）

### 板上旧镜像的真实行为

```text
# journalctl -u asterinas-desktop-m5-network -b --no-pager | tail -20
Apr 13 19:38:38 ... Starting asterinas-desktop-m5-network.service ...
Apr 13 19:38:45 ... Main process exited, code=exited, status=1/FAILURE
Apr 13 19:38:45 ... Failed with result 'exit-code'.
（约 7 s 结束，但 unit 仍记 failed）

# journalctl -u asterinas-dns-shim -b --no-pager | tail -20
Apr 13 19:38:38 ... Started asterinas-dns-shim.service ...
Apr 13 19:38:52 ... megrez-dns-shim[58]: dns shim unconfigured: ASTERINAS_DESKTOP_DNS_HOST is unset or invalid
Apr 13 19:38:52 ... Main process exited, code=exited, status=3/NOTIMPLEMENTED
Apr 13 19:38:52 ... Failed with result 'exit-code'.
```

对应 `origin/main` 的旧代码：m5 unit `TimeoutStartSec=180s` +
`RemainAfterExit=yes`；dns unit `Type=simple` + `Restart=always` +
`RestartPreventExitStatus=3`（未配置 forwarder 时直接变成 failed）。

### 本轮改动（`fix/board-ux-b`）

`build_rootfs.sh`：

- `asterinas-desktop-m5-network.service`：
  `TimeoutStartSec` **180 s → 45 s**，`Restart=no`，
  `SuccessExitStatus=75`，删除 `RemainAfterExit=yes`；
- `browser_web_network.conf` 的 `TimeoutStartSec` **600 s → 45 s**；
- `asterinas-dns-shim.service`：`Restart=no` +
  `SuccessExitStatus=1 3`（exit 3 = 未配置 forwarder，exit 1 = 隧道不可达，
  都是明确 inactive，不再 failed/重启）。
  **没有改成 `Type=oneshot`**：健康路径下 shim 是常驻转发进程
  （`megrez_dns_shim.py` 的 `serve()` 永不返回），oneshot 会一直停在
  “activating”，而 service 默认 `Before=basic.target`，会把 boot 卡住。
  用 `Type=simple` + 成功退出码 + `Restart=no` 得到了“失败即 inactive、
  不重启”的同一效果，且健康时 boot 不受影响。

`desktop_m5_network_evidence.sh`：

- 自有 fixture 的 curl 加 `--connect-timeout 1`
  （`ASTERINAS_DESKTOP_M5_FIXTURE_CONNECT_TIMEOUT_SECONDS`，默认 1 s）；
  1 s 连不上即认定 fixture 不在，不再耗尽传输预算；
- `web_fail` 区分环境与证据错误：链路/地址/邻居/可达/DNS/HTTP(S) 传输或
  超时 → **exit 75**；配置错误、内容长度/摘要/HTTP 状态等与 fixture 矛盾
  → exit 1。配合 unit 的 `SuccessExitStatus=75` 与
  `RemainAfterExit=no`，**fixture 缺失时 unit 以 inactive 结束**，不进
  `systemctl --failed`、也不重启；真正的证据矛盾仍然 failed。

### 测试

焦点用例（全部通过）：

```text
test_builder_installs_m4_desktop_and_m5_network_evidence ... ok
test_builder_installs_daily_use_console_for_firefox ... ok
test_web_network_failure_taxonomy ... ok
test_dns_shim_unit_does_not_loop_or_fail_without_a_forwarder ... ok
test_builder_drops_unsupported_sysctl_keys ... ok
test_guest_evidence_keeps_link_diagnostics_bounded_to_cached_probes ... ok
```

新增断言：unit 的 45 s / `SuccessExitStatus=75` / `Restart=no` / 无
`RemainAfterExit`；DNS 不重启/失败退出码放行；`web_fail` 分类（75 vs 1）；sysctl 两条
赋值被注释且其它行保留。

全量 `python3 -m unittest tools.riscv.tests.test_debian_m5_network`
（57 tests）剩 4 个失败/错误，并已用 `git stash` 在**未改动的
origin/main** 上复现同样失败，**不是本轮引入**：

- `test_qemu_web_bootargs_are_split_below_uboot_console_limit`
  （`NetworkM5QemuOperations` 缺 `config` 属性）；
- `test_desktop_core_evidence_does_not_probe_netsurf`
  （`desktop_m4_evidence.sh:39` 的 PROBE timeout，环境性）；
- `test_profile_extends_m4_with_exact_network_identity`
  （profile 列表 `xdotool` 重复，既有）；
- `test_qemu_adapter_adds_only_one_slirp_virtio_net_device`
  （断言 `'bochs-display'` 精确元素，实际是 `bochs-display,xres=...`，既有）。

`test_debian_browser_web` 也有 2 个在干净 `origin/main` 上同样失败的用例
（display provider resolver、startup timeline 顺序），与本轮文件无交集。

### 判定

**代码 ok；真机验证 pending。** 当前板子仍是旧镜像，unit 文件不会变；
需要重建镜像（或把 unit/脚本覆盖到镜像）后，复测
`systemctl --failed`、m5 的退出时间与 `is-active`、dns 的 `is-active`。
本轮**未刷 eMMC、未改 boot 分区**。

## 复现

- 原始日志：`/tmp/board_b2.log`（sysctl 配置/探针）、
  `/tmp/board_b3.log`（写测试与 systemd-sysctl journal）、
  `/tmp/board_b4.log`（getty journal、`setsid -c`）、
  `/tmp/board_b5.log`（`setsid -w -c` 退出码）。
- 手动 agetty 记录：`/tmp/board_b1.log` 的 `### console_manual` 段。
- 所有命令均短行分步；串口单写者；未 reboot、未刷机、未改 boot 分区。
