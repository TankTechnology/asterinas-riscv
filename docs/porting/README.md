# Milk-V Megrez RISC-V 移植状态

> **本页是唯一实时状态入口。** 可执行命令只维护在
> [`tools/riscv/README.md`](../../tools/riscv/README.md)；冻结结果只维护在
> [证据索引](evidence/megrez-history-index.md)及其日期页中。

## 当前状态

| 项目 | 当前值 |
|---|---|
| 状态来源 | 本文件所在 Git commit |
| 工作分支 | `codex/megrez-porting-handoff` |
| 最近真机候选 | `3ef99e6bd15341578b32256c897050e873ca2547` |
| 最近真机记录 | [`megrez-3ef99e6bd153-20260719T173554Z`](evidence/2026-07-20-megrez-pid1-recovery.md) |
| 当前目标 | 建立可见、可交互的最小 console 闭环 |

当前结论：Asterinas 已在 Megrez 上通过默认 Sv48、OSTD、三个辅助 hart、
rootfs 和 PID 1，并完成第一次用户态 `write`。这仍是集成与调试成果，
**不代表 Asterinas 已正式支持 Megrez**。

## 最后真机边界

- PID 1 已进入 U-mode，完成首次 page fault、`openat`，且
  `write(fd=1, requested=50)` 返回 50。
- 普通用户态 hello 没有出现在 UART 原始日志中。
- Asterinas 没有收到 framebuffer，因此该次运行不存在 HDMI 输出路径。
- 同一受控会话随后出现新的 DDR → OpenSBI → U-Boot 序列并回到 `=>`；
  观察窗口内无人执行外部复位。
- 原始串口流没有时间戳，timer 也没有触发标记，因此“400 秒 timer 导致
  恢复”的归因还依赖受控会话记录；它不能覆盖 timer 与 SBI 都停止的状态。

完整身份、哈希与限制见
[最新真机证据](evidence/2026-07-20-megrez-pid1-recovery.md)。

## 最近 QEMU 边界

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

第一缺失边界是 **console route**，不是 Sv48、OSTD、rootfs、`exec` 或首次
用户态 syscall：写入已经成功，但没有内核 UART console；同时没有
framebuffer 后端可把 `tty0` 内容显示到 HDMI。

## 当前单变量假设

只验证一个假设：**通过显式 framebuffer handoff，安全复用 U-Boot 已初始化
的 scanout，可以先让 Asterinas 的 `tty0` 在 HDMI 上可见。**

这一假设要求同时明确 framebuffer 的地址、大小、格式、stride、物理内存
保留和 RISC-V 可用的 cache 策略；它不等于移植完整 Eswin 显示驱动。

> **更新（2026-08-12）**：该假设的软件链已在 QEMU `virt` 上通过——
> 显式 handoff 后 framebuffer 不再返回 `None`，`tty0` 内容由 VT 渲染到
> 画面（见 [riscv-qemu-desktop.md](riscv-qemu-desktop.md)）。真实 EIC7700
> 的 HDMI scanout、cache/coherency 与厂商固件行为仍需板卡验证。

## 尚未解决的问题

1. RISC-V framebuffer handoff 在 QEMU `virt` 已通过（不再是 `None`，见
   [riscv-qemu-desktop.md](riscv-qemu-desktop.md)）；真实 EIC7700 的
   framebuffer 交接仍待板卡验证。
2. framebuffer 物理内存保留和 cache 策略尚未实现；当前 WC 路径会在
   RISC-V 上 panic。
3. Megrez DTB 描述的是 DesignWare APB UART；当前组件不支持这一路径。
4. 冻结的诊断 initramfs 只是 marker 程序，不是 BusyBox 交互 shell。
5. 串口输入 → shell → `tty0` 显示的完整路由尚未建立。

不要把 DTB 中的 `snps,dw-apb-uart` 伪装成 `ns16550a`；错误的寄存器步长和
访问宽度可能让轮询停在错误寄存器上。

## 下一次 QEMU 门禁

**暂停，尚未授权执行。** 下一轮详细测试设计需先与用户讨论；届时至少要
验证 DT 解析、framebuffer 区间保留、映射/cache 拒绝路径、VT 像素变化、
shell I/O 和进程清理。QEMU `virt` 只能验证软件链路，不能证明 EIC7700 的
显示、cache/coherency、厂商固件或复位行为。

## 下一次真机门禁

**暂停，尚未授权执行。** 只有冻结候选身份、QEMU 门禁通过、产物地址与
校验完成，并且整个观察窗口都有独立外部复位或断电能力时，才讨论一次受控
`booti`。不得覆盖 RockOS 文件、不得 `saveenv`，每份产物只执行一次
`booti`。

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
