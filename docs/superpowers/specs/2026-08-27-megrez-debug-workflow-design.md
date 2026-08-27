# Megrez 仿真优先调试流水线设计

## 目标

把当前分散的构建产物检查、QEMU 验证、XMODEM 传输、U-Boot 命令和串口证据收敛为一条可重复的流水线。日常修改应先在宿主机或 QEMU 中排除与硬件无关的问题；真机只承担 EIC7700 时钟、复位、MMIO、DMA、PHY、HDMI 和真实 USB 等不可仿真的差异验证。

成功标准如下：

- 一个不可变的启动清单绑定 kernel、initramfs、QEMU DTB、Megrez DTB、地址、bootargs、文件长度、SHA-256 和 CRC32。
- 一个用户入口提供 `check`、`simulate` 和 `board` 三个阶段；每个阶段生成同一种 JSON 结果和完整日志。
- `board` 自动管理串口独占、当前波特率、分块传输、CRC、paced U-Boot 命令、启动里程碑和定时重启，不要求操作者复制命令或反复复位。
- 未改变的大文件在板上 RAM 的长度和 CRC 匹配时不重复传输。
- 真机失败必须被分类为 transport、U-Boot、kernel、guest 或 hardware，不能只报告“超时”。

## 非目标

- QEMU 不声称模拟 EIC7700 DWMAC、PHY、HDMI、USB 控制器或板级时钟/复位。
- 不通过 Linux 启动来代替 Asterinas 验证。
- 不修改 U-Boot 持久环境，不发送 `saveenv`。
- 不在每次内核修改后运行完整桌面门禁；桌面门禁是较慢的里程碑验证。

## 方案比较

### 方案 A：薄调度层和共享启动清单（采用）

保留现有 `megrez_xmodem.py`、`megrez_board_session.py`、QEMU Debian 门禁和物理 GMAC 门禁。新增小型 contract/runner：contract 只负责清单与阶段结果，runner 只负责组合现有组件。

优点是复用已验证边界、失败容易归属、可逐步迁移；缺点是底层模块仍然存在，但用户只需要一个入口。

### 方案 B：把所有功能继续塞进 `megrez_board_session.py`

入口数量少，但该文件已经同时承担串口、U-Boot、DTB、里程碑和多种传输。继续扩展会把 QEMU、构建和发布也耦合进来，难以独立测试，因此拒绝。

### 方案 C：使用 Makefile/Bash 串联现有命令

实现快，但无法可靠地持有串口状态、处理信号、区分波特率或原子发布证据；也难以对失败恢复做确定性测试，因此只保留 Make target 作为 Python 入口别名，不把状态机写在 shell 中。

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

### 真机状态机

真机阶段固定为：

1. 独占打开串口并检测 115200 或 1.5 Mbps 的真实 U-Boot 行提示符。
2. 对每个 artifact 先执行地址加精确长度的 CRC32；一致则记录 cache hit。
3. 不一致时第一份文件完成一次波特率切换，后续文件保持 1.5 Mbps XMODEM-1K；每份完成后重新验证长度与 CRC32。
4. 使用 paced TX 设置非持久 bootargs、patch 当前 DTB，并执行一次 `booti`。
5. 捕获完整串口，按严格顺序记录 kernel 和 guest marker。
6. 成功或失败都关闭串口并原子发布日志/result；Asterinas 的 `reboot_after` 负责恢复 U-Boot。

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

## 实施里程碑

1. M1：实现清单 contract、stage result 和 `plan/check`；所有测试为宿主机秒级。
2. M2：实现 PTY U-Boot/XMODEM 仿真和单命令 `board --dry-run`，冻结完整动作序列。
3. M3：接入现有 QEMU fast gate，结果绑定 plan hash。
4. M4：接入真机 cache-aware XMODEM 与 BoardSession，完成一次命令启动。
5. M5：把 GMAC、USB、HDMI 等真机 gate 作为可选 profile，默认不扩大验证范围。

M1–M4 完成后，日常调试不再依赖手工串口命令；新增硬件功能只需要实现一个独立 profile，而不是复制整条启动流程。
