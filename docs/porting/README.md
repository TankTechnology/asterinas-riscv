# Milk-V Megrez RISC-V 移植状态

> **本页是唯一实时状态入口。** 可执行命令只维护在
> [`tools/riscv/README.md`](../../tools/riscv/README.md)；冻结结果只维护在
> [证据索引](evidence/megrez-history-index.md)及其日期页中。

## 当前状态

| 项目 | 当前值 |
|---|---|
| 状态来源 | 本文件所在 Git commit |
| 工作分支 | `codex/megrez-dwmac-board-current` |
| 最近真机候选 | `6b7ffa04c` |
| 最近真机记录 | [Debian browser-quality rootfs 与验收](evidence/2026-08-31-megrez-debian-browser-quality.md) |
| 当前目标 | 在不重复刷写 rootfs 的前提下完成物理鼠标 M8，再单独评估 Firefox |

当前结论：Asterinas 的 compiled Sv39 内核已在 Megrez 上启动 4 个 hart，
通过 MMC 与 Stage1 进入持久 Debian 13.6 riscv64 根；systemd 257.13、udev、
logind、Xorg fbdev、双 xHCI、USB 键盘、PCManFM、Openbox、NetSurf 与 xterm
已在真机达到桌面和浏览器 M7 边界。板载 RJ45 的 M5 网络闭环也已通过，
QEMU 进一步通过了 M8 轻量浏览器质量门。物理 M8 仍因未连接鼠标而降级。
这仍是集成与调试成果，**不代表 Asterinas 已正式支持 Megrez**，也不代表
原生 DRM 加速、现代 JavaScript 或 Firefox 已可用。

## 最后真机边界

- RockOS 只通过同交换机网络把哈希冻结的 Image、Stage1 和安装器放到
  `/boot`；Asterinas 自己把签名 browser-quality 1 GiB 镜像写入 eMMC 分区 2，
  完成全分区 SHA-256 后由 Asterinas/SBI 重启。此次安装结果为
  `DEBIAN_INSTALL_PASS`，没有绕过 Asterinas 使用 Linux 写入根分区。
- 真机重新加载并核对 Image、Stage1 和 Megrez DTB，进入 Asterinas Sv39、
  4 hart、MMC、Debian 13.6 systemd 257.13 与 1920x1080 firmware framebuffer。
- 两套 DWC3/xHCI 控制器登记物理键盘；Xorg、Matchbox、PCManFM、NetSurf
  和 xterm 到达 M4/M7。板载 RJ45 的 M5 链路、固定代理 HTTPS、图片资源和
  20 请求压力闭环也已通过。
- 本轮未连接物理鼠标，Xorg 明确报告缺少 pointer device；因此物理 M8
  点击、下载和长时间浏览门禁没有完成，结果保留为 `guest-timeout`，而不是
  把缺失的 M8 标记误写成通过。

完整身份、哈希、失败诊断和限制见
[最新真机证据](evidence/2026-08-31-megrez-debian-browser-quality.md)。

## 最近 QEMU 边界

- 与真机相同的签名 Desktop M4 根已在 QEMU、compiled Sv39、4 hart、
  2 GiB、无网络条件下启动 PCManFM、NetSurf、xterm 与 Matchbox，门禁保存
  1280x1024 非空截图并返回 `passed: true`。见
  [Desktop M4 应用证据](evidence/2026-08-26-debian-desktop-m4-apps.md)。
- Debian 13.6 browser-quality 根在 QEMU、compiled Sv39、4 hart 下通过了
  M6/M7 网络浏览器门禁和 M8 轻量质量门：CJK/Latin、PNG、表单、滚动、前进
  后退、256 KiB 下载、120 秒进程存活和 163995 字节截图均有 marker。见
  [browser-quality 证据](evidence/2026-08-31-megrez-debian-browser-quality.md)。
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

当前第一缺失边界是 **真机指针交互与 Firefox**：板载 RJ45、键盘、桌面和
NetSurf 百度访问已经通过，QEMU 的 M8 交互质量门也已通过；真机没有插鼠标，
M8 的表单、链接、下载和长时间浏览点击无法完成。显示仍使用 U-Boot 交接的
firmware framebuffer，不是原生 EIC7700 DRM 或加速渲染。

## 当前单变量假设

下一轮只验证一个假设：**当前物理 USB mouse 的相对移动和按键事件能持续
经过 xHCI/HID worker、evdev 和 Xorg，到达 Matchbox/NetSurf 窗口。** QEMU
已经验证精确移动、左键、窗口切换和 M8 内容操作；真机只补可观察的光标、
表单、链接和下载行为，不在同一轮扩展热插拔或 DRM。

## 尚未解决的问题

1. 真机光标移动和点击仍需操作者确认；USB 热插拔和任意 report-protocol
   HID 尚未验证。
2. Firefox/Gecko 尚未启动门禁；当前 NetSurf 的 JavaScript 只达到
   `limited-pass`，不能外推为现代网页兼容性。
3. EIC7700 原生 DRM、cache/coherency、加速渲染与显示模式切换尚未实现；
   当前依赖 RAM-only 1920x1080 firmware framebuffer handoff。
4. systemd 仍会报告缺少 kmod、部分 clone/syscall 与 cgroup 语义，但这些
   警告没有阻止本次 udev/logind/非 root 桌面 READY。
5. 音频、视频、GPU 加速和更长时间的桌面性能尚未测量。

不要把 DTB 中的 `snps,dw-apb-uart` 伪装成 `ns16550a`；错误的寄存器步长和
访问宽度可能让轮询停在错误寄存器上。

## 下一次 QEMU 门禁

QEMU 的 M6/M7/M8 已通过；只有 Firefox、输入、网络或根文件系统发生相关
变化时，才在同一冻结根上跑对应的窄门禁，避免反复重跑已冻结的结果。

## 下一次真机门禁

不重刷已经通过安装校验的 rootfs，也不重复 M5/M6/M7。下一次只在接入物理
鼠标后，用同一冻结 plan 做有界真机 M8：确认光标、表单、链接、下载和进程
存活；若无鼠标则保留当前 `guest-timeout` 证据。后续仍不得 `saveenv`，也
不得从 Linux 绕过 Asterinas 修改 Debian 根分区。

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
