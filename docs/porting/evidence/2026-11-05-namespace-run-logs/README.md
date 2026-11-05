# Namespace run logs（归档）

这些文件原本存放在 `tools/riscv/pidns/` 与 `tools/riscv/nsmatrix/`，是
KE-M5…M7 命名空间验证时保留的控制台判定输出。2026-11-05 归档到证据目录：
`tools/` 只保留可重复执行的工具与测试源码，运行输出按日期归到 `docs/`。

- 来源：`tools/riscv/pidns/*.log`（10 个）、`tools/riscv/nsmatrix/*.log`（4 个）。
- 内容：每次运行的 `PASS/FAIL` 判定、LTP 子集 verdict、路径探测输出。
- 引用它们的报告：`docs/porting/reports/KE-M5-report.md`、`KE-M6-report.md`、
  `KE-M7-report.md`（链接已指向本目录）。
- 测试源码仍在原处：`tools/riscv/pidns/*.c`、`tools/riscv/nsmatrix/*.c`。

日志是当时的运行产物，不是当前状态；当前入口见
[`tools/riscv/LAYOUT.md`](../../../tools/riscv/LAYOUT.md) 与
[`docs/porting/README.md`](../../README.md)。
