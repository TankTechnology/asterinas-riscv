# Megrez 仿真优先调试流水线设计

## 目标

把当前分散的构建产物检查、QEMU 验证、XMODEM 传输、U-Boot 命令和串口证据收敛为一条可重复的流水线。日常修改应先在宿主机或 QEMU 中排除与硬件无关的问题；真机只承担 EIC7700 时钟、复位、MMIO、DMA、PHY、HDMI 和真实 USB 等不可仿真的差异验证。

成功标准如下：

- 一个不可变的启动清单绑定 kernel、initramfs、QEMU DTB、Megrez DTB、地址、bootargs、文件长度、SHA-256 和 CRC32。
- 一个用户入口提供 `check`、`simulate` 和 `board` 三个阶段；每个阶段生成同一种 JSON 结果和完整日志。
- `board` 自动管理串口独占、当前波特率、分块传输、CRC、paced U-Boot 命令、启动里程碑和定时重启，不要求操作者复制命令或反复复位。
- 未改变的大文件在板上 RAM 的长度和 CRC 匹配时不重复传输。
- 真机失败必须被分类为 transport、U-Boot、kernel、guest 或 hardware，不能只报告“超时”。
- 同一份已经通过 QEMU 的 plan 只需一条 `board` 命令即可上板；缓存命中时最多执行一次 Asterinas `booti`，并在五分钟总 deadline 内发布结构化结果。
- 成功或失败后由 `asterinas.reboot_after` 自动返回 U-Boot。流水线不把人工复位当作正常控制流，也不因等待自动恢复而阻塞其他宿主机诊断。

## 非目标

- QEMU 不声称模拟 EIC7700 DWMAC、PHY、HDMI、USB 控制器或板级时钟/复位。
- 不通过 Linux 启动来代替 Asterinas 验证。
- 不修改 U-Boot 持久环境，不发送 `saveenv`。
- 不在每次内核修改后运行完整桌面门禁；桌面门禁是较慢的里程碑验证。
- 第一阶段不把 1 GiB Debian rootfs 通过 XMODEM 重传。桌面 profile 必须复用已经单独安装并有身份结果的持久 rootfs。

## 方案比较

### 方案 A：薄调度层和共享启动清单（采用）

保留现有 `megrez_xmodem.py`、`megrez_board_session.py`、QEMU Debian 门禁和物理 GMAC 门禁。新增小型 contract/runner：contract 只负责清单与阶段结果，runner 只负责组合现有组件。

优点是复用已验证边界、失败容易归属、可逐步迁移；缺点是底层模块仍然存在，但用户只需要一个入口。

### 方案 B：使用 Makefile/Bash 串联现有命令

实现快，但无法可靠地持有串口状态、处理信号、区分波特率或原子发布证据；也难以对失败恢复做确定性测试，因此只保留 Make target 作为 Python 入口别名，不把状态机写在 shell 中。

### 方案 C：常驻板端调试代理和远程电源控制

它可以处理彻底卡死和带外复位，但当前没有已经验证的远程电源控制器，而且会在 Asterinas 基础启动闭环之前引入新的常驻服务和权限边界。本阶段延后该方案；自动恢复失败时只发布 `recovery-timeout`，不假装能够复位硬件。

## 分层验证模型

| 层级 | 典型耗时 | 解决的问题 | 不解决的问题 |
|---|---:|---|---|
| Host contract | 秒级 | 清单、路径、哈希、CRC、命令构造、串口分片、超时、信号与结果发布 | 内核能否启动 |
| QEMU fast | 分钟内 | Sv39、SMP=4、内核/initramfs 组合、系统调用、VirtIO 网络、DNS/TCP/TLS、guest marker | EIC7700 板级硬件 |
| QEMU desktop | 里程碑运行 | Xorg、窗口、NetSurf、鼠标键盘和 framebuffer 证据 | 真机 HDMI/USB/GMAC |
| Megrez physical | 最小必要次数 | DWMAC/PHY、SDHCI、真实 USB、HDMI、DMA 和固件交接 | 通用软件错误不应留到此层 |

默认开发循环运行 Host contract 和相关的 QEMU fast。只有影响应用/图形会话的里程碑才运行 QEMU desktop；只有硬件驱动或准备发布时才运行 Megrez physical。

## 架构

### 启动清单

新增 `tools/riscv/megrez_debug_contract.py`，定义 frozen 数据类型：

- `ArtifactIdentity`：逻辑名称、绝对规范路径、加载地址、长度、SHA-256、CRC32。
- `DebugPlan`：schema、profile、四个 artifact identity、bootargs、SMP、Sv39、预期 marker、自动重启秒数。
- `StageResult`：阶段、passed、reason、plan SHA-256、开始/结束时间和证据路径。

JSON 使用固定 schema、排序键和重复 key 拒绝。读取 artifact 时只打开一次普通非 symlink 文件，从同一文件描述符计算长度、SHA-256 和 CRC32。

### 统一入口

新增 `tools/riscv/megrez_debug.py`：

- `plan`：从命令行 artifact 创建清单，不访问串口或网络。
- `check`：验证清单、工具和上一阶段证据；运行秒级 contract tests。
- `simulate --tier fast|desktop`：调用现有 QEMU gate，并把结果绑定到清单 hash。
- `board DEVICE`：要求至少有匹配清单的 fast 结果；串行调用现有 XMODEM/U-Boot/物理 gate 边界。

runner 不复制 QEMU 参数构造、XMODEM 算法或浏览器 classifier。子步骤通过注入的 operations 接口测试，生产默认使用现有模块。

当前仓库已经完成 M1 和 M2：`plan/check`、canonical contract、PTY/XMODEM 仿真和 `board --dry-run` 已存在。真实 `simulate` 尚未接入，非 dry-run 的 `board` 当前明确返回 `plan-board-not-implemented`。M3/M4 必须补齐这两个边界，而不是再新增一套旁路脚本。

`simulate --tier fast` 是日常默认入口；`simulate --tier desktop` 只在 Debian、网络、Xorg 或浏览器发生变化时运行。`board` 的 TCP probe profile 只要求匹配的 fast 结果；后续 browser profile 必须同时要求匹配的 desktop 结果和已经安装到板载存储的 rootfs 身份结果。

M3/M4 只完成现有 schema v1 的 `tcp-probe` 闭环，不把 1 GiB rootfs 硬塞进当前 64 MiB artifact contract。M6 若引入 browser profile，必须提升 contract schema，并显式加入 U-Boot、Stage1、root image、root manifest、packages lock、package checksums 和“已安装 rootfs”阶段结果；schema v1 文件继续可读且不被静默改义。

### 真机状态机

真机阶段固定为：

1. 独占打开串口并检测 115200 或 1.5 Mbps 的真实 U-Boot 行提示符。
2. 对每个 artifact 先执行地址加精确长度的 CRC32；一致则记录 cache hit。
3. 不一致时第一份文件完成一次波特率切换，后续文件保持 1.5 Mbps XMODEM-1K；每份完成后重新验证长度与 CRC32。
4. 使用 paced TX 设置非持久 bootargs、patch 当前 DTB，并执行一次 `booti`。
5. 捕获完整串口，按严格顺序记录 kernel 和 guest marker。
6. 成功或失败都关闭串口并原子发布日志/result；Asterinas 的 `reboot_after` 负责恢复 U-Boot。

整个状态机使用一个五分钟 monotonic deadline。artifact CRC 检查、必要的 XMODEM、`booti`、marker 捕获和自动恢复共享这一个预算；任何子步骤都不得重新开始完整 deadline。第一次终止信号触发有序关闭和证据发布，第二次终止信号立即退出。

任何步骤都不自动发送板级 reset。若自动重启未在 deadline 内返回，只报告 `recovery-timeout`，由后续带电源控制的独立里程碑处理。

## 错误分类与证据

稳定 reason 前缀为：

- `plan-*`：输入身份或仿真证据不匹配。
- `transport-*`：串口、波特率、XMODEM 或 CRC。
- `uboot-*`：提示符、命令回显、FDT 或 `booti`。
- `kernel-*`：进入内核后 panic/oops/超时。
- `guest-*`：Asterinas 已启动但用户态 marker 或网络探针失败。
- `hardware-*`：仅由具体 GMAC/USB/HDMI/SDHCI gate 产生。

每次运行先删除 stale `result.json`，日志和结果使用同目录临时文件、fsync 和原子替换。失败结果不得包含 `passed: true`。

## 测试策略

- contract 单测冻结 JSON schema、one-open 身份、CRC/SHA、Sv39/SMP=4 和非法路径。
- runner 单测使用 fake operations 覆盖 cache hit、首文件切波特率、后续同波特率、CRC mismatch、分片提示符、命令丢字、信号清理和 stale evidence。
- PTY 集成测试模拟 U-Boot 的两种 `loadx` 完成协议，不需要开发板。
- QEMU fast 使用最小 probe initramfs，验证 Asterinas 内核和 guest TCP/HTTP；完整 Debian 桌面不进入默认循环。
- 真机验收只重复一次已在 QEMU/PTY 通过的 plan，并要求硬件专属 marker。
- board 集成测试使用假 U-Boot/PTY 证明五分钟总预算不会在子阶段重置，并覆盖“全部缓存命中时零传输、一次 booti、自动恢复”的主路径。

## 后续 NetSurf 与 Debian 兼容阶段

M3/M4 完成后，再用同一流水线推进浏览器和基础内核兼容，避免把真机作为应用调试环境：

1. QEMU desktop 首先把 URL 从百度 logo PNG 改为 `https://www.baidu.com/`，要求 NetSurf 显示 logo、搜索框和基础文字，并能提交普通搜索；不把登录、动态热搜或现代 Web API 作为 NetSurf 3.11 的成功条件。
2. QEMU 串口证据记录 NetSurf 最终 URL、标题、进程状态、页面截图以及独立的 DNS/HTTPS 结果。页面不完整时先区分浏览器上游能力、Debian 用户态错误和 Asterinas 内核缺口。
3. `systemd-sysusers` 必须先记录准确退出码和 errno；`/proc/sys/fs/nr_open` 写入的 `EOPNOTSUPP` 使用独立内核回归测试修复。没有根因证据时不通过 guest 脚本隐藏 systemd 失败。
4. QEMU browser 结果绑定 kernel、Stage1、rootfs manifest、packages lock 和截图。只有该结果通过后，browser profile 才允许单命令上板，真机仅重新验证板载 GMAC、HDMI 和真实 USB 输入。

NetSurf 3.11 的 JavaScript/DOM 支持有限，因此 browser profile 区分“基础页面可用”和“现代 JavaScript 完整兼容”。后者需要 Firefox 或其他现代浏览器里程碑，不由本设计虚报。

## 实施里程碑

1. M1：实现清单 contract、stage result 和 `plan/check`；所有测试为宿主机秒级。
2. M2：实现 PTY U-Boot/XMODEM 仿真和单命令 `board --dry-run`，冻结完整动作序列。
3. M3：接入现有 QEMU fast gate，结果绑定 plan hash。
4. M4：接入真机 cache-aware XMODEM 与 BoardSession，完成一次命令启动。
5. M5：把 GMAC、USB、HDMI 等真机 gate 作为可选 profile，默认不扩大验证范围。
6. M6：在完成 M3/M4 后接入完整百度首页 QEMU browser 证据，并修复由证据证明的 Debian/Asterinas 基础兼容问题。

M1–M4 完成后，日常调试不再依赖手工串口命令；新增硬件功能只需要实现一个独立 profile，而不是复制整条启动流程。
