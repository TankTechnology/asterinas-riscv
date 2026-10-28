# Milk-V Megrez RISC-V 当前状态

> 本页只是状态导航，不是独立验收证据。当前板端事实、镜像身份和已知问题
> 以 [`board-image-current.md`](board-image-current.md) 为准；可执行命令以
> [`tools/riscv/README.md`](../../tools/riscv/README.md) 和
> [`tools/riscv/MAINTAINABILITY.md`](../../tools/riscv/MAINTAINABILITY.md)
> 为准；冻结结果只在 [`evidence/megrez-history-index.md`](evidence/megrez-history-index.md)
> 及其日期页中追加。

## 当前主线

2026-10-28 的板端状态是**单镜像、单启动流程**：

| 区域 | 当前内容 |
|---|---|
| p1 `/boot` | RockOS 启动文件 + 一个 Asterinas daily 启动项 |
| p2 | 一个 `browser-web` Debian rootfs（3 GiB） |
| p3 | RockOS 恢复系统 + 一个当前 Asterinas generation |
| 桌面 | Debian systemd + Xorg fbdev + openbox/PCManFM/lxpanel + Firefox ESR |
| 工具 | software rendering；GPU/PowerVR 线暂停 |

- 镜像身份、构建命令、板端验收结果和已知问题见
  [`board-image-current.md`](board-image-current.md)。
- host 测试基线和清理决策见
  [`tools/riscv/MAINTAINABILITY.md`](../../tools/riscv/MAINTAINABILITY.md)。
- 当前命令入口：`make test_riscv_host HOST_TEST_JOBS=32`，以及
  `Makefile` 中的 `test_riscv_*` / `run_riscv_megrez_desktop` targets。

## 当前边界

当前板端仍有三类已知问题，不能从 QEMU 通过外推：

1. guest 的 `/etc/resolv.conf` 仍可能停留在构建容器地址，板端 DNS 需要
   独立验证；
2. 实验网网关和宿主直连外网条件不是稳定前提，不能把 QEMU slirp 通过
   当作板端独立联网证明；
3. Firefox 冷启动、桌面输入和网络路径必须分别在真机上验证，单次冒烟
   不等于 qualification。

具体命令、镜像 SHA、串口 marker 和失败边界以
[`board-image-current.md`](board-image-current.md) 为准。

## 证据与历史

- 当前证据索引：[`evidence/megrez-history-index.md`](evidence/megrez-history-index.md)
- 历史状态页（2026-09-20 快照）：[`archive/2026-09-20-status-readme.md`](archive/2026-09-20-status-readme.md)
- 历史启动指南：[`megrez-asterinas-boot-guide.md`](megrez-asterinas-boot-guide.md)
- 历史启动流程可视化：[`megrez-boot-flow.html`](megrez-boot-flow.html)
- 设计、计划与审查史：[`docs/superpowers/`](../superpowers/)
- 归档的旧研究材料：[`tools/riscv/archive/README.md`](../../tools/riscv/archive/README.md)

`evidence/` 是追加式冻结证据，不随文档整理而删除或改写。历史长文只用于
理解概念和失败模式，不维护当前命令。
