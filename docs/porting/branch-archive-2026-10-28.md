# 远端分支归档（2026-10-28）

本文件随 `archive/2026-10-28` 分支一起推送：该分支是一个以 main 树为内容、
以被删除的 134 个分支 tip 为父提交的合并提交，因此这些分支的提交仍可从
该分支取回（`git log archive/2026-10-28` / `git checkout archive/2026-10-28 -- <path>`）。

| 分支 | PR | 状态 | 末次提交 | 说明 |
|---|---|---|---|---|
| `chore/gitignore-local-artifacts` | #201 | 已合并 | 2026-10-21 | 内容已在 main |
| `chore/x86-debian-cleanup` | #200 | 已合并 | 2026-10-21 | 内容已在 main |
| `codex/debian-riscv-input` | #78 | 已关闭 | 2026-08-24 | PR 已关闭；抽查其特性已在 main |
| `codex/debian-rootfs-m1` | #80 | 已合并 | 2026-08-24 | 内容已在 main |
| `codex/debian-runtime-netlink` | — | 无 PR | 2026-10-11 | 无 PR；netlink generic 已在 main，信号 SA_RESETHAND 测试与 ext4 门禁脚本未落地 |
| `codex/discussion-3761-photo` | — | 无 PR | 2026-09-27 | 无 PR；仅一张 Megrez 桌面截图文档 |
| `codex/ext2-metadata-sync-pr` | #180 | 已关闭 | 2026-10-07 | PR 已关闭；抽查其特性已在 main |
| `codex/firefox-desktop-objective` | #178 | 已关闭 | 2026-09-25 | PR 已关闭；抽查其特性已在 main |
| `codex/firefox-startup-compat` | — | 无 PR | 2026-09-05 | 末次提交内容与 main 一致 |
| `codex/fix-browser-timeline` | #207 | 已合并 | 2026-10-27 | 内容已在 main |
| `codex/fix-loongarch-vdso-baseline` | #171 | 已合并 | 2026-09-24 | 内容已在 main |
| `codex/fix-main-rustfmt` | #170 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/icmp-echo-current-stack` | #169 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/ip-socket-netns` | #149 | 已关闭 | 2026-09-23 | PR 已关闭；抽查其特性已在 main |
| `codex/lmbench-icmp-integration` | — | 无 PR | 2026-09-24 | 末次提交内容与 main 一致 |
| `codex/lmbench-native-archive-verification` | #167 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/lmbench-native-results` | #144 | 已合并 | 2026-09-24 | 内容已在 main |
| `codex/lmbench-network-main-ready` | #175 | 已合并 | 2026-09-24 | 内容已在 main |
| `codex/loopback-broadcast-regression` | #172 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/megrez-browser-performance-evidence` | #176 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/megrez-browser-startup` | #143 | 已关闭 | 2026-09-23 | PR 已关闭；抽查其特性已在 main |
| `codex/megrez-desktop-only` | — | 无 PR | 2026-09-03 | 末次提交内容与 main 一致 |
| `codex/megrez-dwmac-board-current` | — | 无 PR | 2026-09-01 | 末次提交内容与 main 一致 |
| `codex/megrez-ready-service` | #163 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/netlink-socket-netns` | #150 | 已关闭 | 2026-09-23 | PR 已关闭；抽查其特性已在 main |
| `codex/netlink-table-netns` | #151 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/netns-poll-lifecycle` | #152 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/pr179-current-main-port-20261007` | #183 | 已关闭 | 2026-10-07 | PR 已关闭；抽查其特性已在 main |
| `codex/proc-net-dev` | #146 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/riscv-ktest-exit-status` | — | 无 PR | 2026-09-23 | 末次提交内容与 main 一致 |
| `codex/riscv-network-regression-harness` | #174 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/riscv-ptrace-regset` | #177 | 已合并 | 2026-09-24 | 内容已在 main |
| `codex/route-dump-current-stack` | #164 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/route-local-table-current-stack` | #166 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/route-lookup-current-stack` | #165 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/shebang-script-argv` | #168 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/socket-ifconf` | #145 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/socket-ifreq` | #147 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/socket-ioctl-netns` | #148 | 已关闭 | 2026-09-23 | PR 已关闭；抽查其特性已在 main |
| `codex/socket-readv-prefault` | #158 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/socket-recv-deferred-fault` | #155 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/socket-writev-atomic` | #159 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/stream-send-iov-fault` | #162 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/tcp-err-linux-semantics` | #173 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/tcp-msg-dontwait-send` | #161 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/tcp-recv-copy-fault` | #156 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/tcp-recv-prefault` | #154 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/tcp-recv-wrap-fault` | #157 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/udp-msg-dontwait` | #160 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/udp-recv-prefault` | #153 | 已关闭 | 2026-09-24 | PR 已关闭；抽查其特性已在 main |
| `codex/udp-rpc-instrument` | — | 无 PR | 2026-09-24 | 末次提交内容与 main 一致 |
| `docs/drm-shared-core` | #202 | 已合并 | 2026-10-21 | 内容已在 main |
| `docs/x86-experience-followup` | #199 | 已合并 | 2026-10-21 | 内容已在 main |
| `docs/x86-matrix-acpi` | #194 | 已合并 | 2026-10-21 | 内容已在 main |
| `docs/x86-riscv-reuse` | #197 | 已合并 | 2026-10-21 | 内容已在 main |
| `feat/acpi-s5` | #193 | 已合并 | 2026-10-21 | 内容已在 main |
| `feat/debian-daily-gate` | #198 | 已合并 | 2026-10-21 | 内容已在 main |
| `feat/debian-serial-login` | #191 | 已合并 | 2026-10-21 | 内容已在 main |
| `feat/drm-l3-3d-render` | — | 无 PR | 2026-09-20 | 末次提交内容与 main 一致 |
| `feat/drm-l3-virgl-reached` | — | 无 PR | 2026-09-21 | 末次提交内容与 main 一致 |
| `feat/drm-r3-gem-render-node` | #142 | 已合并 | 2026-09-21 | 内容已在 main |
| `feat/fs-network-stack-validation` | #179 | 已合并 | 2026-10-11 | 内容已在 main |
| `feat/highres-cpu-posix-timers` | #128 | 已合并 | 2026-09-04 | 内容已在 main |
| `feat/ipv6-dual-stack-sockets` | #137 | 已关闭 | 2026-09-27 | PR 已关闭；抽查其特性已在 main |
| `feat/locked-memory-accounting` | #124 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `feat/mincore-residency` | #123 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `feat/ptrace-attach-detach` | #115 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `feat/riscv-high-resolution-timers` | #120 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `feat/riscv-mmap-populate-pagemap` | #121 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `feat/sched-rr-get-interval` | #106 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `feat/virtio-console-poll-input` | #196 | 已合并 | 2026-10-21 | 内容已在 main |
| `firefox-production-mainline` | — | 无 PR | 2026-08-31 | 末次提交内容与 main 一致 |
| `fix/brk-fragmented-heap` | #110 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `fix/chmod-setgid-semantics` | #131 | 已合并 | 2026-09-04 | 内容已在 main |
| `fix/clocksource-concurrent-update` | #118 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `fix/clone3-args-size` | #111 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `fix/clone3-set-tid-permissions` | #112 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `fix/debian-sigpwr-power` | #195 | 已合并 | 2026-10-21 | 内容已在 main |
| `fix/dev-zero-mmap` | #113 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `fix/ext2-fixture-blocks` | #220 | 已合并 | 2026-10-28 | 内容已在 main |
| `fix/fcntl-dupfd-equal-minimum` | #99 | 已合并 | 2026-09-03 | 内容已在 main |
| `fix/frame-allocator-pressure-reclaim` | #133 | 已合并 | 2026-09-04 | 内容已在 main |
| `fix/getcpu-current-cpu` | #107 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `fix/membarrier-smp-semantics` | #104 | 已合并 | 2026-09-04 | 内容已在 main |
| `fix/memfd-exec-write-denial` | #130 | 已合并 | 2026-09-04 | 内容已在 main |
| `fix/mmap-file-error-ordering` | #109 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `fix/mmap-write-only-permissions` | #105 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `fix/posix-range-lock-deadlock` | #135 | 已关闭 | 2026-09-05 | PR 已关闭；抽查其特性已在 main |
| `fix/prctl-thread-name-truncate` | #114 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `fix/pty-lock-semantics` | #129 | 已合并 | 2026-09-04 | 内容已在 main |
| `fix/riscv-flush-icache-arch-gate` | #102 | 已合并 | 2026-09-03 | 内容已在 main |
| `fix/riscv-flush-icache-online-harts` | #100 | 已合并 | 2026-09-03 | 内容已在 main |
| `fix/riscv-smp-affinity-migration` | #103 | 已关闭 | 2026-09-03 | PR 已关闭；抽查其特性已在 main |
| `fix/sched-setaffinity-permissions` | #108 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `fix/scheduler-policy-compat` | #117 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `fix/scheduler-set-permissions` | #116 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `fix/sigaction-reset-hand-metadata` | #134 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `fix/syscall-dynamic-clock-safety` | #127 | 已合并 | 2026-09-04 | 内容已在 main |
| `fix/x86-boot-marker` | #190 | 已合并 | 2026-10-21 | 内容已在 main |
| `fs/ea-inode` | #188 | 已合并 | 2026-10-21 | 内容已在 main |
| `fs/ext4-features` | #187 | 已合并 | 2026-10-21 | 内容已在 main |
| `fs/punch-hole` | #186 | 已合并 | 2026-10-21 | 内容已在 main |
| `fs/sticky-bit` | #192 | 已合并 | 2026-10-21 | 内容已在 main |
| `fs/tmpfile-orphan-list` | #185 | 已合并 | 2026-10-20 | 内容已在 main |
| `integrate-firefox-current-main-20260828` | — | 无 PR | 2026-08-28 | 末次提交内容与 main 一致 |
| `integrate/firefox-origin-main-20260828` | — | 无 PR | 2026-08-28 | 末次提交内容与 main 一致 |
| `integrate/riscv-ltp-six-failures` | #126 | 已合并 | 2026-09-04 | 内容已在 main |
| `integration-upstream-debian-full-20261009` | — | 无 PR | 2026-10-21 | 无 PR；10/21 Debian 集成快照，内容已被 main 覆盖 |
| `test/riscv-ltp-clone-thread` | #119 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `test/riscv-ltp-locked-memory` | #125 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `test/riscv-ltp-memory-expansion` | #122 | 已关闭 | 2026-09-04 | PR 已关闭；抽查其特性已在 main |
| `track/a-browser-js-admission` | #84 | 已关闭 | 2026-08-27 | PR 已关闭；抽查其特性已在 main |
| `track/a-browser-m5-builder` | #86 | 已关闭 | 2026-08-27 | PR 已关闭；抽查其特性已在 main |
| `track/a-browser-m5-pretitle-diagnostic` | — | 无 PR | 2026-08-28 | 末次提交内容与 main 一致 |
| `track/a-browser-m5f19-combined` | — | 无 PR | 2026-08-28 | 末次提交内容与 main 一致 |
| `track/a-browser-m5f20-late-window` | — | 无 PR | 2026-08-28 | 末次提交内容与 main 一致 |
| `track/a-debian-app-admission` | #81 | 已合并 | 2026-08-26 | 内容已在 main |
| `track/a-firefox-online-web` | — | 无 PR | 2026-08-28 | 末次提交内容与 main 一致 |
| `track/b-browser-kernel-foundation` | #82 | 已合并 | 2026-08-27 | 内容已在 main |
| `track/b-private-network-current-main` | #87 | 已合并 | 2026-08-27 | 内容已在 main |
| `track/b-proc-sandbox-status` | — | 无 PR | 2026-08-28 | 末次提交内容与 main 一致 |
| `track/b-scm-atomic-drop` | — | 无 PR | 2026-08-28 | 末次提交内容与 main 一致 |
| `track/b-scm-rights-b1` | #89 | 已合并 | 2026-08-27 | 内容已在 main |
| `track/b-scm-rights-b1-datagram` | #92 | 已合并 | 2026-08-28 | 内容已在 main |
| `track/b-scm-rights-b1-policy-split` | #93 | 已合并 | 2026-08-28 | 内容已在 main |
| `track/b-scm-rights-b1-stream` | #91 | 已合并 | 2026-08-28 | 内容已在 main |
| `track/b-scm-rights-b1-stream-acyclic` | — | 无 PR | 2026-08-28 | 末次提交内容与 main 一致 |
| `track/b-seccomp-filter-chain` | #85 | 已合并 | 2026-08-27 | 内容已在 main |
| `track/b-seccomp-tsync` | #83 | 已合并 | 2026-08-27 | 内容已在 main |
| `track/b-set-tid-address-current-main` | #88 | 已合并 | 2026-08-27 | 内容已在 main |
| `track/drm` | #53 | 已关闭 | 2026-08-18 | PR 已关闭；抽查其特性已在 main |
| `track/drm-m20` | — | 无 PR | 2026-08-23 | 末次提交内容与 main 一致 |
| `track/nixos` | — | 无 PR | 2026-08-23 | 末次提交内容与 main 一致 |
| `x86-interactive-login` | #189 | 已合并 | 2026-10-21 | 内容已在 main |

## 保留的分支

`main`、8 个有 open PR 的分支（#139 #140 #141 #182 #203 #223 #224 #225），
以及 `dns-shim-isolated-console-fix` —— 它带着未落地的 PowerVR/pvr 驱动工作
（`kernel/src/device/dri/powervr_bridge.rs` 等 12 个文件、+2283/−144），需要保留。
