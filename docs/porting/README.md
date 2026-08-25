# Milk-V Megrez RISC-V 移植状态

> **本页是唯一实时状态入口。** 可执行命令只维护在
> [`tools/riscv/README.md`](../../tools/riscv/README.md)；冻结结果只维护在
> [证据索引](evidence/megrez-history-index.md)及其日期页中。

## 当前状态

| 项目 | 当前值 |
|---|---|
| 状态来源 | 本文件所在 Git commit |
| 工作分支 | `codex/megrez-debian-storage` |
| 最近真机候选 | `6576d661f` |
| 最近真机记录 | [Debian systemd M2 双启动](evidence/2026-08-25-megrez-debian-systemd-m2.md) |
| 当前目标 | 修复 systemd 基础兼容性缺口，再扩展设备与桌面能力 |

当前结论：Asterinas 的 compiled Sv39 内核已在 Megrez 上启动 4 个 hart，
通过 MMC 与 Stage1 进入持久 Debian Trixie 根，并由 systemd 257.13 完成
boot 1、用户态重启和 boot 2 PASS。这仍是集成与调试成果，**不代表
Asterinas 已正式支持 Megrez**，也不代表 HDMI 桌面已可用。

## 最后真机边界

- RockOS 只把哈希冻结的 Image、Stage1 和安装器放到 `/boot`；Asterinas
  自己把签名 Debian 1 GiB 镜像写入 eMMC 分区 2，并完成全分区 SHA-256。
- 两次正式启动都重新加载并核对 Image、Stage1 和 Megrez DTB，进入
  Asterinas Sv39、4 hart、MMC 与 Debian systemd 257.13。
- boot 1 在约 40 秒由 Debian `/sbin/reboot -f` 进入新 OpenSBI/U-Boot
  周期；boot 2 在约 32 秒输出持久计数 PASS。
- 真机仍报告 `Framebuffer not found`；因此串口 systemd 成功不能外推为
  HDMI、DRM 或桌面成功。

完整身份、哈希、失败诊断和限制见
[最新真机证据](evidence/2026-08-25-megrez-debian-systemd-m2.md)。

## 最近 QEMU 边界

- current-main DRM R1 已在 QEMU 10.2.1、compiled Sv39、4 hart、2 GiB、
  无网络环境通过硬件光标 set/move/hide 门禁；用户态 marker 与 VirtIO-GPU
  host trace 严格对应。见
  [DRM R1 证据](evidence/2026-08-26-drm-r1-current-main.md)。这证明的是
  VirtIO-GPU 软件路径，不代表 EIC7700/Megrez HDMI。
- 相同的 Debian systemd M2 产物已在 QEMU `virt`、compiled Sv39、4 hart、
  2 GiB、无网络、无显示条件下通过两次启动和持久 boot-count gate。见
  [M2 构建与 QEMU 证据](evidence/2026-08-25-debian-systemd-m2-build.md)。
- 冻结提交 `7f691c479df1b5319f71a6ad738f36541d90ca54` 的默认 Sv48
  Image 已通过通用 U-Boot `booti` 的 timer 与 panic 两个软件恢复场景；
  两者都进入新的 OpenSBI/U-Boot 周期。见[冻结恢复证据](evidence/2026-07-18-riscv-software-reboot-qemu.md)。
- `70734c14e` 的 direct QEMU 已在 16 GiB、4 hart、Sv48/Svade 下到达
  用户态 marker 并完成进程清理；它没有经过 U-Boot，也不是 EIC7700
  真机证据。见[证据索引](evidence/megrez-history-index.md)。
- 重构后 `main` 的 Sv39 内核已在 QEMU `virt` + bochs-display 上跑通完整
  framebuffer 显示链：U-Boot 注入 `simple-framebuffer` 节点，内核登记
  framebuffer 且 VT 控制台渲染到 1280x1024 画面。见
  [riscv-qemu-desktop.md](riscv-qemu-desktop.md)。这验证的是软件链，
  不代表 EIC7700/Megrez 的显示硬件行为。

这三条 QEMU 结果与 `3ef99e6bd` 真机候选属于不同产物和环境，不得拼成同一
Image 的连续运行。

## 第一缺失边界

Debian/systemd 的第一缺失边界已经从“能否进入 PID 1”推进到 **基础 mount
与 service 兼容性**：`/run/lock` 和 `/tmp` mount unit 会报告 `protocol`
失败，systemd-logind、configfs 和部分 sysusers 路径仍不工作。UART console
已经可交互，但 EIC7700 framebuffer/DRM 仍未接入。

## 当前单变量假设

下一轮只验证一个假设：**Stage1 已准备的 `/tmp`、systemd 的 tmp.mount 与
Asterinas 的 mount/mountinfo 通知语义之间存在不一致，导致挂载实际状态和
systemd/libmount 观察结果不同。** 先在 QEMU 记录 mount syscall 返回值与
`/proc/self/mountinfo`，再决定修 Stage1 交接还是内核语义；不同时扩展桌面。

## 尚未解决的问题

1. `/run/lock`、`/tmp` mount unit 和 libmount watch 语义仍需定位。
2. systemd-logind、configfs、sysusers、kmod 与若干 syscall/clone 路径尚缺。
3. PCI/网络、USB/xHCI/HID 尚未纳入这次 Megrez systemd gate。
4. RISC-V framebuffer handoff 在 QEMU `virt` 已通过；真实 EIC7700 的
   framebuffer、DRM、HDMI、cache/coherency 仍待实现和真机验证。
5. 当前 Debian 根是基础 systemd profile，不含完整桌面和现代浏览器栈。

不要把 DTB 中的 `snps,dw-apb-uart` 伪装成 `ns16550a`；错误的寄存器步长和
访问宽度可能让轮询停在错误寄存器上。

## 下一次 QEMU 门禁

不重复已经通过的 M2 双启动。下一轮在同一冻结根上只增加 mount/mountinfo
诊断，要求 `/tmp` 与 `/run/lock` 的真实挂载状态和 systemd 观察一致；修复
后再运行一次有针对性的 systemd gate。

## 下一次真机门禁

当前 systemd 双启动真机门禁已经通过，不重复相同 `booti`。只有某个基础
兼容性修复先在 QEMU 形成明确 PASS/FAIL 标记后，才冻结新 Image 并做一次
对应真机复验；仍不得 `saveenv`，也不得从 Linux 绕过 Asterinas 修改 Debian
根分区。

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

## 文档地图与历史归档

- [唯一可执行命令来源](../../tools/riscv/README.md)
- [QEMU framebuffer 显示链（已验证）](riscv-qemu-desktop.md)
- [追加式证据索引](evidence/megrez-history-index.md)
- [最新 PID 1 与恢复证据](evidence/2026-07-20-megrez-pid1-recovery.md)
- [历史启动指南快照](megrez-asterinas-boot-guide.md)
- [历史启动流程可视化快照](megrez-boot-flow.html)
- [`docs/superpowers/` 设计、计划与审查史](../superpowers/)

长指南和 HTML 只用于理解概念、历史决策与失败模式，不维护当前状态或当前
命令。协作者应始终从本页开始，再按需要进入命令页或某一条冻结证据。
