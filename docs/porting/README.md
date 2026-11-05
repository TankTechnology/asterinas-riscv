# Milk-V Megrez RISC-V 移植状态

> **本页是人工维护的状态导航，不是独立验收证据。** 可执行命令只维护在
> [`tools/riscv/README.md`](../../tools/riscv/README.md)；冻结结果只维护在
> [证据索引](evidence/megrez-history-index.md)及其日期页中。任何“当前通过”
> 结论仍须绑定完整 commit、日期、镜像身份和保留的 result/evidence 路径。
>
> **状态时效：** 下面“当前状态 / 最后真机边界”等小节是 2026-09 的验收边界，
> 不代表今天。当前入口见 [tools/README.md](../../tools/README.md)、
> [tools/riscv/LAYOUT.md](../../tools/riscv/LAYOUT.md) 与
> [board-addresses.md](board-addresses.md)；真机性能基线见
> [docs/performance/](../performance/)。

## 2026-10-29 板级更新

这一天的内容记在三份文档里，这里只放结论：

- [桌面与网络打通](2026-10-29-megrez-desktop-network-bringup.md)：浏览器三个故障
  （DNS 死、时钟差半年、端口未认证）、壁纸取不到图的真因（缺
  `/usr/share/mime/mime.cache`）、开机报「Firefox profile 无法加载」的两个原因
  （根分区只读；缺 `~/.mozilla/firefox/profiles.ini`）、引导参数固化到 U-Boot
  `bootcmd`、串口长命令被截断的教训、以及"跑一阵就发不出包"的取证。
- [release 内核与性能基线](2026-10-29-megrez-release-kernel-baseline.md)：dev 对
  release 的 15–22 倍差距；两轮优化在**真机** `rdcycle` 上的结果——第一轮
  `getpid` 5309→3071 周期（−42%），第二轮在板上落在噪声里（QEMU 里那 30% 是
  TCG 放大原子指令的假象）。
- [桌面可视化证据](evidence/2026-10-29-desktop-visual/)：QEMU 与真机截图。

这一天的代码改动：USB HID 输入修复、系统调用信号快路径、唤醒计数改 acquire load、
GMAC datapath 日志降级、镜像六处修复（镜像构建 SIGPIPE 静默退出、陈旧 firefox-esr
包审计、profiles.ini、mime.cache、壁纸/面板、时钟同步）、机器身份
（hostname/os-release/`asterinas-fetch`）、`vim`+`g++`、中文输入法、网络看门狗、
引导看门狗。

## 当前状态


| 项目 | 当前值 |
|---|---|
| 状态来源 | 本文件所在 Git commit |
| 工作分支 | `main` |
| 最近整合 | 合并 `bb76f8d63`（`codex/firefox-daily-use-perf` 的 52 个提交）；验证提交 `d38a0fb28` |
| 最近桌面真机候选 | 验证内核 `8c470f09`；发布 generation `70dd75d0` |
| 最近网络真机候选 | `ed3a6508e`（DWMAC streaming-DMA closure，未随本次合并重跑） |
| 最近真机记录 | [合并后 main 的 QEMU 与真机验证](evidence/2026-09-20-merged-main-physical-validation.md) |
| 当前目标 | 对 `contextSwitchTotalMs` 做一次有界归因观测；在该观测完成前不得再改内核 |

当前结论：合并后的 `main` 已在 QEMU 四 hart 门禁上通过，并在真机上以
`status=pass` 到达桌面（`desktop-ready` 61.8 s，Firefox PID 172，watchdog 已解除），
随后完成一次 `qualified=true` 的 daily-use profile（7/7 功能组，`recovered=true`）。
这证明的是**合并的集成线仍可部署与启动**，不是新的性能结论。

## 最后真机边界

- 冻结的 kernel（`8c470f09`）、Stage1（`9bcf5f7b`）与 Megrez DTB（`465cb129`）
  作为 generation `70dd75d0` 发布到 RockOS 分区 3；`booti` 前逐件校验字节数与 CRC32。
- Sv39、4 hart、fbdev 显示；Stage1 挂载板上 eMMC 分区 2 上**既有的** Debian 根
  （`80b11187`），该根未被重写或重装。
- 有界启动在 95.73 s 内到达 `ASTERINAS_DESKTOP_BOOT_READY`，guest 保持运行；
  随后经 debug console 的软件重启返回 U-Boot。
- daily-use profile 由 Marionette 合成输入完成，`outcome=pass`、`upload_status=0`，
  并有界恢复到新的 U-Boot 周期。**单次 profile 只是冒烟验证**：daily-use 基线按
  定义是封闭的三次运行实验，一次合格运行不会重新资格化它。

## 最近 QEMU 边界

- 四 hart QEMU 图形/控制门禁以合并后的 kernel、重建的 Stage1 与 development
  overlay 根通过：`passed=true`，三个交互周期，`physical=false`。
- 该门禁的输出目录由容器内以 root、mode `0700` 创建，宿主非 root 读不到；
  **读不到不等于空**，必须进容器或提权读取。
- RISC-V kernel-test 套件在合并树上的失败集与 `55ee5c64e` 上**完全相同**
  （`aster_kernel` 2 项、`xarray` 2 项），属既有问题，不是合并引入。

## 第一缺失边界

桌面与 daily-use 路径已经打通，第一缺失边界移到了**性能归因**：唤醒均衡改动把
`runnable-delayed` 机制消除后，三次 B 运行的分类一致为 `mixed`——没有任何边界占
主导。`contextSwitchTotalMs` 在三次 B 运行中仍全部越过 500 ms 诊断阈值，且是最大的
主指标，是目前最明确的下一个观测对象。

网络方面，已验证的板载 DWMAC 路径仍未纳入 Debian/Firefox 门禁；`main` 对
`desktop_m5_network_evidence.sh` 的改动写进了 **rootfs**（由 `build_rootfs.sh`
安装），而本次真机与 QEMU 验证都**没有**重装 rootfs，因此该改动本轮未被验证——
它属于 M5 网络门禁，不属于本条线。

## 当前单变量假设

下一轮只验证一个假设（仍属归因观测，不是优化）：**对 `contextSwitchTotalMs`
做一次有界、开销可测的观测，足以判断缺失的到底是哪一类归因**。
sampled-PC 只有在"缺的是指令区域归因"时才被准入，且其设计稿仍处于待评审状态。

## 尚未解决的问题

1. `contextSwitchTotalMs` 仍越过诊断阈值，且当前分类为 `mixed`；在完成一次有界
   归因观测之前，不得选择第二个内核变量。
2. 唤醒均衡改动的 A/B 未做变体交替、B 组之后也未补 A 对照，因此两组之间的热漂移
   未被独立排除。
3. `main` 的 M5 网络证据脚本改动尚未在任何门禁中验证（rootfs 未重建）。
4. `make check` 在 `main` 上仍有 3 项**既有**失败（`ostd` 两个 dead-code、`dwmac`
   的 "Synopsys" 拼写），不在本次整合范围内。
5. 显示仍使用 U-Boot 交接的 firmware framebuffer，不是原生 EIC7700 显示控制器。

不要把 DTB 中的 `snps,dw-apb-uart` 伪装成 `ns16550a`；错误的寄存器步长和
访问宽度可能让轮询停在错误寄存器上。

## 下一次 QEMU 门禁

不重复已经通过的四 hart 图形/控制门禁。只有当内核或 Stage1 再次变化时才重跑，
并记录 kernel、Stage1、root 三个摘要（旧记录只记了 Stage1）。

## 下一次真机门禁

按 `mixed` 分类的要求，先完成一次有界归因观测，再考虑任何内核变量。若再改内核，
必须按 A/B 协议做 3+3 次合格真机 profile，并在可能时交替变体或补一次 A 对照。
不得 `saveenv`，也不得从 Linux 绕过 Asterinas 修改 Debian 根分区。

## 简化调试记录

后续每轮只记录下面七项，避免同时修改多个变量：

```text
最后成功边界：
第一缺失边界：
当前唯一假设：
单变量测试：
预期 PASS 标记：
停止条件：
证据目录：
```

## 文档地图：活文档 / 日期报告 / 证据 / 历史快照

规则：新结论进“活文档”；一次性实验进按日期命名的报告；原始输出进
`evidence/`；历史快照不再修改，只用于理解当时的决策与失败模式。

### 活文档（随代码更新）

- [工具总索引](../../tools/README.md) · [RISC-V 工具布局](../../tools/riscv/LAYOUT.md)
  · [RISC-V 命令手册](../../tools/riscv/README.md)
- [板子网络地址约定](board-addresses.md)
- [单镜像 / 单启动流程目标](board-image-current.md)
- [上板命令速查](megrez-board-session-commands.md) ·
  [真机运行检查清单](megrez-board-handoff-checklist.md)
- [下游总览](asterinas-riscv-status.md)
- [真机 release 内核与 syscall 基线](../performance/2026-11-05-board-release-baseline.md)

### 按日期报告（只读）

- 本目录 `2026-*.md` 与 `20261029-*.md`：每次实验的冻结记录，命令可复跑，
  结论绑定当时的 commit 与镜像。
- [KE-M2…M7 报告](reports/)：知识工程（KE）阶段的结论与判定日志链接。

### 证据

- [追加式证据索引](evidence/megrez-history-index.md)：按阶段列出已验证边界。
- `evidence/<日期>-<主题>/`：截图、串口日志、result JSON 等原始输出。
- [命名空间运行日志归档](evidence/2026-11-05-namespace-run-logs/)：从
  `tools/` 移出的 14 份判定日志。

### 历史快照（不再更新）

- [启动指南与工程复盘](megrez-asterinas-boot-guide.md) ·
  [启动流程可视化](megrez-boot-flow.html)
- [QEMU 桌面显示链](riscv-qemu-desktop.md) ·
  [真机验证回填设计](riscv-physical-backfill.md) ·
  [`/dev/snd` 设计提案](snd-device-model.md) ·
  [Debian browser M5 准入](debian-browser-m5.md)
- `docs/superpowers/`：设计、计划与审查史。

协作者应始终从本页开始，再按需要进入工具命令页或某一条冻结证据。
