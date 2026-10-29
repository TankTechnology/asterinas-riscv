# 2026-10-29 控制台降噪：DWMAC 状态快照与 HID 心跳

> 目标线：Debian 使用体验（Megrez 真机演示）。
> 范围：只降级诊断打印，不改 ostd 抽象、不引入 `unsafe`、不 push。

## 结论摘要

- `ASTERINAS_GMAC_HW_STATE` 是**真实存在**的周期性诊断打印，已从 `info!` 降为
  `debug!`（`kernel/comps/dwmac/src/device.rs:419`、`:424`）。
- `HID loop alive: polls=...` 在**当前源码树中零命中**。它来自板上那份带临时
  调试插桩的内核镜像；工作树与可检索的 refs 中都没有它，用本树重新构建后该行自然为 0。
  详见“HID 心跳的排查结论”。
- 板级默认启动参数已经是 `loglevel=info`（见
  `tools/riscv/megrez_contract.v1.json` 与
  `tools/riscv/megrez_board_session.py` 的 `DEFAULT_BOOTARGS`），而
  `debug!` 在 `loglevel=info` 下被过滤，所以**默认启动即可静音**。

## 基线（改前）

任务书给出的串口 20 秒采样：

| 类别 | 20 秒条数 |
|---|---|
| `usb-hid`（HID 心跳） | 4 |
| `WARN` | 2 |
| `ASTERINAS_GMAC_HW_STATE` | 0（该 20 秒窗口内无 RX 进展报告；有业务流量时随 RX 进展打印） |

HID 心跳约每 5 秒一条，是这次采样里的绝对多数。

## 排查记录（只读，行号为改动前）

```console
$ grep -rn "ASTERINAS_GMAC_HW_STATE\|HID loop alive" kernel/
kernel/comps/dwmac/src/device.rs:413:  ostd::info!("ASTERINAS_GMAC_HW_STATE stage={} read_failure=true", stage);
kernel/comps/dwmac/src/device.rs:419:  "ASTERINAS_GMAC_HW_STATE stage={} mac_debug=... "
```

`HID loop alive` 的零命中不是 grep 写法问题，已做多路交叉验证：

```console
$ grep -rn "HID loop alive" .                 # 排除 target/ 后 0 命中
$ grep -rn "polls=\|mouse_reports" kernel/ ostd/src/bus/   # 0 命中
$ git grep -n "HID loop\|loop alive" origin/main          # 0 命中
$ git grep -n "HID loop\|loop alive" 9a5034261            # 0 命中（kernel-known-good）
```

### HID 心跳的排查结论

- 日志形如 `usb: usb: HID loop alive: polls=... irq_wakes=... reports=...
  mouse_reports=...`。双重 `usb: ` 说明模块前缀（`kernel/comps/usb/src/lib.rs:15`
  注册的 `"usb: "`）之外，消息正文自身也以 `usb: ` 开头——典型的临时插桩写法。
- 这四个计数器恰好对应本地未提交改动里的 `probe_polls` / `irq_wakes` 以及
  report 计数（`kernel/comps/usb/src/arch/riscv/mod.rs` 的 `PollWakeup` 工作树
  改动），但**工作树与可检索的 refs 中都没有这条打印**。
- 因此：该行只存在于板上那份临时构建的镜像中。用本仓库当前树（或任何已提交
  commit）重新构建板级内核，20 秒采样中 `HID loop alive` 为 **0 条**，无需改代码。
- 如果将来需要在板上临时观察 HID 唤醒路径，请把它写成
  `ostd::debug!("usb: HID loop alive: ...")`，不要用 `info!`；这样默认
  `loglevel=info` 仍然安静，排查时给 `loglevel=debug` 即可打开。

## 方案选择

三选一，针对 DWMAC 的 `ASTERINAS_GMAC_HW_STATE`：

| 方案 | 评价 |
|---|---|
| **a) 改 `debug!`（采用）** | 复用已有 loglevel 机制；改动只有宏名；默认 `loglevel=info` 下静默，`loglevel=debug` 下完整保留。不碰 ostd、不加 unsafe、不加新开关。 |
| b) 保留 `info!` + 运行时开关 | 需要新增 `/proc` 或 cmdline 解析与状态，改动面和维护成本都更大；本任务不需要。 |
| c) 仅在显式动态调试时打印 | 语义上最干净，但需要引入一个新的“动态调试”开关；而 `loglevel` 已经是现成的同类开关，重复造轮子。 |

选择 **a**：它是满足“默认安静、需要时可开”的最小实现，且板级启动参数已经是
`loglevel=info`，不需要改任何启动脚本。

## 改动清单

| 文件:行号（改后） | 改动 |
|---|---|
| `kernel/comps/dwmac/src/device.rs:410` | 给 `log_hardware_state` 增加说明性文档注释 |
| `kernel/comps/dwmac/src/device.rs:419` | `ostd::info!` → `ostd::debug!`（寄存器读取失败分支） |
| `kernel/comps/dwmac/src/device.rs:424` | `ostd::info!` → `ostd::debug!`（完整状态快照） |

调用点保持不变：`device.rs:115`（启动时 `stage=started`）与 `device.rs:596`
（`stage=rx-progress`）。把 `started` 一并降到 `debug` 是有意的——设备上线仍由
`ASTERINAS_GMAC_SELECTED` / `ASTERINAS_GMAC_READY` 两条 `info!` 公告，演示时不丢信息。

没有触碰：

- `ostd/`（未改抽象、未加 `unsafe`）；
- 同一 `rx-progress` 分支里的其他 `ASTERINAS_GMAC_DATAPATH` / `_MTL_RX_LOSS` /
  `_RX_CLASS` 诊断（按 2 的幂阈值触发，量级远小于 HW_STATE；如需进一步降噪可同法处理）；
- HID 路径（当前树无周期打印可改）。

## 串口采样对比

采样方法（板上任选其一）：

```console
# 方式 1：直接抓串口 20 秒
timeout 20 cat /dev/ttyUSB0 > /tmp/ser20.log

# 方式 2：对已有串口日志做 20 秒窗口（配合工具链的 paced bridge 输出）
timeout 20 tail -F /tmp/megrez-serial.log > /tmp/ser20.log
```

计数：

```console
grep -c "HID loop alive" /tmp/ser20.log
grep -c "ASTERINAS_GMAC_HW_STATE" /tmp/ser20.log
grep -c "WARN" /tmp/ser20.log
```

| 采样 | `HID loop alive` | `ASTERINAS_GMAC_HW_STATE` | WARN |
|---|---|---|---|
| 改前（任务书基线，20 s） | 4 | 0（该窗口无 RX 进展） | 2 |
| 改后（预期，默认 `loglevel=info`） | 0 | 0 | 2 |
| 改后（`loglevel=debug`，排查用） | 0（源码无此打印） | 每次 RX 进展一条 | 2 |

> 说明：本次会话无法直连 Megrez 串口（板子与主机网络隔离、需要串口桥），
> 上表“改后”是按代码路径与 loglevel 过滤规则推导的预期值。板端复核命令即上面的
> 三条 `grep -c`。`ostd` 的过滤规则有单元测试覆盖
> （`ostd/src/log/level.rs` 的 `level_ordering` / `level_filter_enabled`）：
> `LevelFilter::Info` 放行 `Info` 及以上，拒绝 `Debug`。

## 演示时静音（一行命令）

**运行时、不动启动参数**（推荐，已登录的 root shell 里直接执行）：

```console
dmesg -n 5
```

内核已实现 Linux 的 `syslog(SYSLOG_ACTION_CONSOLE_LEVEL = 8)` 动作
（`kernel/src/syscall/syslog.rs:50`），`logger` 的 `ConsolePolicy::should_print`
（`kernel/comps/logger/src/klog/store.rs:315`）按 `priority < console_level`
过滤；`5` 表示只放行 warning 及以上，
`info` / `debug` 立即从串口消失。想看回 info 时执行 `dmesg -n 7`，
`dmesg -n 8` 恢复全量（含 debug）。

> 注意：它只改**控制台**阈值，`/dev/kmsg` 的捕获缓冲不受影响；要调整捕获级别
> 用启动参数 `asterinas.klog_capture=...`。

**启动参数方式**（改 U-Boot 环境，重启生效）：

```console
setenv bootargs "console=ttyS0 loglevel=info init=/init"
```

板级默认已经是 `loglevel=info`（`megrez_contract.v1.json`）；要更彻底（连
`info!` 也关掉）用 `loglevel=warn`；需要重新打开 DWMAC 状态快照时用
`loglevel=debug`。

> 当前内核对 `loglevel` 的解析在 `kernel/comps/cmdline/src/early.rs`，支持
> `loglevel=0..8` 与 `off/err/warn/notice/info/debug` 文本形式；最后出现的值生效。
> 现有 Megrez 档位用的是 `info`（contract、preboard、board session）、`warn`
> （QEMU 桌面）或 `off`（Firefox、物理图形），都低于 `debug`，因此降级后的打印
> 在任何现役档位下都不会再出现。目前没有 `/proc/sys/kernel/printk` 节点，但上面的
> `dmesg -n` 走 syscall，等效可用。

## 验收状态

- [x] 打印点已定位并记录 `文件:行号`
- [x] DWMAC 两处已降级为 `debug!`，未改 ostd、未加 `unsafe`
- [x] 静音一行命令已给出（运行时 `dmesg -n 5`；或启动参数 `loglevel=warn`）
- [x] 源码级复核：`HID loop alive` 全树 0 命中，重建后即为 0 条
- [ ] 真机 20 秒采样复核（需在 Megrez 串口上跑上面三条 `grep -c`；本会话无板端访问）
