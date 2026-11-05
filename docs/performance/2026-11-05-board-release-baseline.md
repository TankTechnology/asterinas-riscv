# Megrez 板 syscall 基线（2026-11-05）

日期：2026-11-05

## 前提

板子当前默认内核是 `/root/kernel-rel.img`（sha256 `e2d537…`，即
`/tmp/serve/kernel-alloc.img`，帧分配器性能版）。它**已经是 release 构建**：
`getpid` 2.55 µs，与 dev 构建的 119 µs 相差两个数量级，说明
「dev 构建导致桌面卡顿」这一项在板上已经不存在了。构建产物对比也一致：
dev 的 raw Image 约 17.3 MB，release 约 6.6 MB。

## 基线（release，4 核，桌面正在运行）

`/root/bench`（`tools/riscv/perf/syscall_bench_board.c`，静态编译）：

```text
BENCH getpid                   2552.4 ns
BENCH getppid                  2655.1 ns
BENCH clock_gettime             369.0 ns
BENCH open+close              24045.8 ns
BENCH stat                    14827.1 ns
BENCH pipe write+read 64B      7926.7 ns
BENCH pipe create+close       29490.6 ns
BENCH unix socket create+close 19768.4 ns
BENCH futex wait(0)/wake       4356.7 ns
BENCH fork+wait             4839617.5 ns
```

对照：`docs/performance/2026-11-04-megrez-frame-allocator-pipe.md` 里 dev 构建
的 `getpid` 是 119 µs、`fork+wait` 是 2.3× 优化后的 dev 数值；本表全部是
release 下的现状。

## 本轮没有上板的构建

`main`（含 origin/main 合并与 epoll 环修复）的 release 构建：

- 镜像：`/tmp/serve/kernel-release-merge.img`
- sha256：`c7f9fc0c9dddaf5e24407dc39459330dd5dd5eb095657f85481e7a870515eb5`
- 大小：6,643,928 B（`make kernel TARGET_ARCH=riscv64 RELEASE=1`，49.9 s）

没有部署：板子当前状态已经可用，且当天反复重启已在 p2 上留下一个坏目录项
（旧 `/home/asterinas` 里的 `Xorg.0.log` 变成 `Stale file handle`，整个 HOME
已改名到 `/home/asterinas.bak-20261105` 并新建了干净的 `/home/asterinas`）。
需要最新内核时，把它拷成 `/root/kernel-rel.img` 再重启即可。

## 结论

- 性能方向不需要再来一轮 dev→release：板上已是 release。
- 下一步若继续做性能，应从 `fork+wait`（4.84 ms）、`pipe create+close`
  （29.5 µs）、`open+close`（24.0 µs）这三个热点入手，而不是构建模式。
