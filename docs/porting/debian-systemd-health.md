# Debian systemd 健康度（L2）

## 方法

- guest 探针：`tools/x86/debian/systemd_health.sh`（注入到
  `/usr/local/bin/systemd-health`），由 `daily_use_gate.sh --systemd-health`
  在串口登录后以 root 执行；
- 串口协议：每行 `SYSD <category> …`，本轮共 15 类：`systemd`、`version`、
  `is_system_running`、`uptime`、`analyze`、`analyze_raw`、`blame`、
  `critical`、`verify`、`running_services`、`units_total`、`unit_files`、
  `failed_count`、`jerr_count`、`jerr`；出现失败 unit 时还会输出
  `failed`/`failed_result`/`failed_evid`；最后 `SYSD_DONE` 结束会话；
- host 解析：剥掉 ANSI 颜色后写成
  `target/systemd-health/systemd-health/` 下的 `startup.tsv`、`failed.tsv`、
  `blame.tsv`、`critical.tsv`、`verify.tsv`、`journal-errors.tsv` 与
  `health.json`；
- 探针先检查 `/run/systemd/system`：若 PID 1 不是 systemd，只记录
  `SYSD systemd=no` 并停止，不编造数字。

## 运行方式（一条命令）

```sh
make test_x86_debian_systemd_health \
  X86_DEBIAN_KERNEL=$PWD/target/prime/kernels/x86.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/final-matrix/xwrap/image18.ext2
```

整轮墙钟 **21 s**（含复制镜像、启动、登录、探测、关机）。

## 环境

| 项 | 值 |
|---|---|
| 日期 | 2026-10-23 |
| 架构 / 加速 | x86_64 / KVM（4 vCPU、4 GiB） |
| 宿主 HEAD | `2cb9c39cc`（L2 改动见本提交） |
| 内核 | `target/prime/kernels/x86.elf`（2026-10-23 构建，sha256 前缀 `9e8ee5dd544fa03a`） |
| 根镜像 | `target/final-matrix/xwrap/image18.ext2` |
| 发行版 | Debian trixie，systemd `257.13-1~deb13u1` |
| PID 1 | `systemd`（`SYSD systemd=yes`） |
| 探测时 uptime | 5.94 s |

## 启动时间

| 阶段 | 秒 |
|---|---|
| firmware | **0.000** |
| loader | **0.000** |
| kernel | **0.303** |
| userspace | **3.112** |
| 总计 | **3.416** |

原始行（`analyze_raw`）：

```
Startup finished in 303ms (kernel) + 3.112s (userspace) = 3.416s
```

说明：vmm-direct 启动没有 firmware/loader 阶段，systemd 的 analyze 行直接
省略这两项，本表按 0 计；kernel 阶段由 systemd 从内核时间戳推算，userspace
含 initrd 交接后的全部 unit 启动。`graphical.target` 在 **2.280 s** 到达
（critical-chain 排名第 4 行）。

## 失败 unit

**0 个。** `systemctl --failed` 输出为空，`systemctl is-system-running`
返回 `running`（不是 `degraded`）。

| unit | 状态 | 归因 | journal 证据 |
|---|---|---|---|
| — | — | 无失败 unit | `systemctl --failed` 空；`is-system-running=running` |

`systemd-analyze verify` 的 3 个关键 unit 全部通过：

| unit | rc | 输出 |
|---|---|---|
| multi-user.target | 0 | 无 |
| getty@.service | 0 | 无 |
| ssh.service | 0 | 无 |

## 运行中 service / unit 总数

| 指标 | 数值 |
|---|---|
| 运行中 service（loaded + running） | **9** |
| 已加载 unit（`list-units --all`） | **156** |
| 已安装 unit file（`list-unit-files`） | **249** |

## 最慢的 20 个 unit（`systemd-analyze blame`）

| 排名 | unit | 秒 |
|---|---|---|
| 1 | dpkg-db-backup.service | 0.852 |
| 2 | ldconfig.service | 0.636 |
| 3 | modprobe@drm.service | 0.194 |
| 4 | modprobe@efi_pstore.service | 0.188 |
| 5 | modprobe@fuse.service | 0.179 |
| 6 | systemd-udevd.service | 0.177 |
| 7 | systemd-modules-load.service | 0.164 |
| 8 | systemd-remount-fs.service | 0.149 |
| 9 | systemd-random-seed.service | 0.146 |
| 10 | run-lock.mount | 0.146 |
| 11 | systemd-tmpfiles-setup-dev-early.service | 0.143 |
| 12 | modprobe@configfs.service | 0.141 |
| 13 | systemd-tmpfiles-setup.service | 0.140 |
| 14 | systemd-udev-load-credentials.service | 0.136 |
| 15 | systemd-udev-trigger.service | 0.133 |
| 16 | systemd-journal-flush.service | 0.111 |
| 17 | systemd-user-sessions.service | 0.109 |
| 18 | systemd-journal-catalog-update.service | 0.093 |
| 19 | systemd-journald.service | 0.092 |
| 20 | systemd-sysusers.service | 0.090 |

最慢的 `dpkg-db-backup.service` 也只用了 0.85 s，没有分钟级的 unit。

## 关键链（`critical-chain` 前 15 行，去掉 2 行说明和 1 行空行）

```
graphical.target @2.280s
└─multi-user.target @2.278s
  └─getty.target @2.276s
    └─getty@tty1.service @2.274s
      └─systemd-user-sessions.service @2.077s +109ms
        └─basic.target @1.972s
          └─sockets.target @1.971s
            └─systemd-hostnamed.socket @1.969s
              └─sysinit.target @1.935s
                └─systemd-update-done.service @1.867s +64ms
                  └─ldconfig.service @1.219s +636ms
                    └─systemd-tmpfiles-setup.service @1.056s +140ms
```

## boot 期 error 日志（`journalctl -p err -b`）

本 boot 共 **3 条** error 级 journal entry（4 行输出；rsyslog 的失败是
多行 entry）。按消息聚合（数字归一化为 `#`）：

| 次数 | 消息 | 归因分类 |
|---|---|---|
| 1 | `card0: Failed to write 'add' to /sys/class/drm/card0/uevent: Input/output error` | **syscall/内核接口缺失**：DRM sysfs 的 `uevent` 属性被注册为只读（`kernel/core/src/device/drm_sysfs.rs:204-215`），udev 的写 `add` 无法触发合成 uevent，返回 EIO。 |
| 1 | `renderD128: Failed to write 'add' to /sys/class/drm/renderD128/uevent: Input/output error` | 同上（render 节点）。 |
| 1 | `imklog: cannot open kernel log (/proc/kmsg): No such file or directory.` | **syscall/内核接口缺失**：内核未提供 `/proc/kmsg`（只有 `/dev/kmsg`）。 |
| 1 | `activation of module imklog failed [v8.2504.0 try https://www.rsyslog.com/e/2145]` | 上一条的后果；rsyslogd 仍正常启动并改用 imuxsock。 |

这两类 error 都**没有让任何 unit 进入 failed 状态**，属于非致命的内核接口
缺口。

## 测量口径修正（本轮发现的夹具 bug）

1. `systemd-analyze blame` 输出 `620ms` 时，最初的 awk 把 `ms` 当秒，读出
   “620 s” 的假数字；已同时支持 `min`/`s`/`ms`，修正后与 critical-chain 的
   `ldconfig +636ms` 对齐；
2. 串口内核日志会插入被截断的 `ESC[`，通用的 CSI 正则会把紧随其后的
   `SYSD` 的首字母 `S` 吃掉，导致 3 条 blame 行丢失；改为只剥离 SGR
   （`ESC[…m`）后 20 条全部保留；
3. `systemd-analyze` 的 kernel 阶段是 `303ms`，原解析只认 `s` 后缀，已支持
   `ms`。

三条修正都在脚本里固化；上表数字来自修正后的最终轮。

## 结论：健康度评级 **A**

- **systemd 状态**：`running`，0 个 failed unit，3 个关键 unit verify 全部
  rc=0；启动总时长 3.416 s，最慢 unit 0.852 s；
- **扣分项**（非致命）：2 类内核接口缺口会写 error 日志——
  DRM sysfs `uevent` 只读（UDEV 无法触发合成事件）与 `/proc/kmsg` 缺失
  （rsyslog imklog 无法读内核日志）；
- **最该修的 3 个**：
  1. 给 `/sys/class/drm/{card0,renderD128}/uevent` 提供可写的合成 uevent
     属性，消掉 udev 的 EIO；
  2. 实现 `/proc/kmsg`（或让 rsyslog 默认禁用 imklog），消掉 rsyslog 的
     两条 error；
  3. 观测覆盖：firmware/loader 阶段在 vmm-direct 下恒为 0，且当前只 verify
     了 3 个 unit；建议把 verify 扩到全部 unit，补齐启动阶段归因（这是覆盖
     缺口，不是功能缺陷）。

## 复现产物

`target/systemd-health/console.txt`（串口全量，含 41 条 `SYSD ` 行）、
`target/systemd-health/systemd-health/*.tsv`、`health.json`。
