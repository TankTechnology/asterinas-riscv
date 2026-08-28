# Milk-V Megrez RISC-V 移植状态

> **本页是唯一实时状态入口。** 可执行命令只维护在
> [`tools/riscv/README.md`](../../tools/riscv/README.md)；冻结结果只维护在
> [证据索引](evidence/megrez-history-index.md)及其日期页中。

## 当前状态

| 项目 | 当前值 |
|---|---|
| 状态来源 | 本文件所在 Git commit |
| 工作分支 | `codex/drm-r1-current-main` |
| 最近真机候选 | `f3d9c73fc` |
| 最近真机记录 | [Debian Desktop M4 应用](evidence/2026-08-26-debian-desktop-m4-apps.md) |
| 当前目标 | 固化鼠标交互，再接入原生网络并逐步替换 framebuffer 路径 |

当前结论：Asterinas 的 compiled Sv39 内核已在 Megrez 上启动 4 个 hart，
通过 MMC 与 Stage1 进入持久 Debian Trixie 根；systemd 257.13、udev、
logind、Xorg fbdev、双 xHCI、USB 键盘和鼠标、Matchbox、PCManFM、NetSurf
与 xterm 已在无自动重启的真机启动中到达完整 M4 READY。HDMI 桌面与串口
调试可同时保留。这仍是集成与调试成果，**不代表 Asterinas 已正式支持
Megrez**，也不代表原生 DRM 加速或网络已经可用。

## 最后真机边界

- RockOS 只通过同交换机网络把哈希冻结的 Image、Stage1 和安装器放到
  `/boot`；Asterinas 自己把签名 Desktop M4 1 GiB 镜像写入 eMMC 分区 2，
  完成全分区 SHA-256 后由 Asterinas/SBI 重启。
- 真机重新加载并核对 Image、Stage1 和 Megrez DTB，进入 Asterinas Sv39、
  4 hart、MMC、Debian systemd 257.13 与 1920x1080 firmware framebuffer。
- 两套 DWC3/xHCI 控制器分别登记物理鼠标和键盘；Xorg 通过 evdev 选择
  两者，随后 Matchbox、PCManFM、NetSurf 和 xterm 到达完整 M4 READY。
- 有界启动在 READY 后由 180 秒保护定时器回到新 U-Boot 周期；随后的长期
  启动删除了该定时器，重复到达 READY，并把桌面留在运行状态。

完整身份、哈希、失败诊断和限制见
[最新真机证据](evidence/2026-08-26-debian-desktop-m4-apps.md)。

## 最近 QEMU 边界

- 与真机相同的签名 Desktop M4 根已在 QEMU、compiled Sv39、4 hart、
  2 GiB、无网络条件下启动 PCManFM、NetSurf、xterm 与 Matchbox，门禁保存
  1280x1024 非空截图并返回 `passed: true`。见
  [Desktop M4 应用证据](evidence/2026-08-26-debian-desktop-m4-apps.md)。
- current-main 的签名 Debian Desktop M3 已在 QEMU、compiled Sv39、4 hart、
  2 GiB、无网络环境通过非 root Xorg fbdev + evdev + Matchbox + xterm 门禁，
  并保存 1280x1024 非空截图。见
  [Desktop M3 current-main 证据](evidence/2026-08-26-debian-desktop-m3-current-main.md)。
  这不代表 Megrez 物理 framebuffer、HDMI 或 xHCI 已通过。
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

基础桌面第一缺失边界已经推进到 **真机鼠标交互与网络**：鼠标已通过
xHCI、HID、evdev 并被 Xorg 选中，但物理移动/点击仍需 HDMI 操作者确认；
Asterinas 网络尚未接入这个 Debian 根，因此 NetSurf 目前只证明应用和窗口
启动，不能外推为网页访问。显示仍使用 U-Boot 交接的 firmware framebuffer，
不是原生 EIC7700 DRM 或加速渲染。

## 当前单变量假设

下一轮只验证一个假设：**当前物理 USB boot mouse 的相对移动和按键事件能
持续经过中断驱动 xHCI/HID worker、evdev 和 Xorg，到达 Matchbox 窗口。**
QEMU 已验证精确移动/左键事件；真机只补操作者可观察的光标移动和窗口点击，
不在同一轮扩展网络、热插拔或 DRM。

## 尚未解决的问题

1. 真机光标移动和点击仍需操作者确认；USB 热插拔和任意 report-protocol
   HID 尚未验证。
2. PCI/板载网络尚未纳入 Desktop M4 门禁，NetSurf 还不能访问网页。
3. EIC7700 原生 DRM、cache/coherency、加速渲染与显示模式切换尚未实现；
   当前依赖 RAM-only 1920x1080 firmware framebuffer handoff。
4. systemd 仍会报告缺少 kmod、部分 clone/syscall 与 cgroup 语义，但这些
   警告没有阻止本次 udev/logind/非 root 桌面 READY。
5. NetSurf 是轻量浏览器且不代表现代 JavaScript 浏览器兼容性；音频也未测。

不要把 DTB 中的 `snps,dw-apb-uart` 伪装成 `ns16550a`；错误的寄存器步长和
访问宽度可能让轮询停在错误寄存器上。

## 下一次 QEMU 门禁

不重复已经通过的 M4 应用启动和鼠标事件。只有网络或输入代码发生相关变化
时，才在同一冻结根上跑对应的窄门禁。

## 下一次真机门禁

当前 M4 应用真机门禁已经通过，不重复相同 `booti`。下一次只由操作者移动
并点击已经连接的鼠标，确认 HDMI 光标/窗口行为；通过后转向网络。后续仍
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

## 文档地图与历史归档

- [唯一可执行命令来源](../../tools/riscv/README.md)
- [Megrez 网络硬件研究契约](evidence/megrez-network-hardware-research-contract.md)
- [Megrez/EIC7700 网络硬件资料账本](evidence/megrez-network-hardware-source-ledger.md)
- [QEMU framebuffer 显示链（已验证）](riscv-qemu-desktop.md)
- [追加式证据索引](evidence/megrez-history-index.md)
- [最新 PID 1 与恢复证据](evidence/2026-07-20-megrez-pid1-recovery.md)
- [历史启动指南快照](megrez-asterinas-boot-guide.md)
- [历史启动流程可视化快照](megrez-boot-flow.html)
- [`docs/superpowers/` 设计、计划与审查史](../superpowers/)

长指南和 HTML 只用于理解概念、历史决策与失败模式，不维护当前状态或当前
命令。协作者应始终从本页开始，再按需要进入命令页或某一条冻结证据。
