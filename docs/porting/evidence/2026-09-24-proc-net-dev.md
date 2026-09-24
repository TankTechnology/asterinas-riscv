# Network device statistics for native LMBench metadata

The pinned `asterinas/lmbench` native `make results` run completed all 109
measurement groups, but its result text printed three
`cannot open /proc/net/dev` warnings. This patch adds the Linux-shaped
`/proc/net -> self/net` path and `/proc/[pid]/net/dev`, scoped to the target
task's network namespace. The device file has the standard two headers and
16 numeric columns per interface. RX/TX packet and byte counters are backed
by atomic per-interface statistics; the 12 error, drop, and other columns are
zero until their events are instrumented. This is a partial statistics
implementation, not a claim of complete Linux device accounting.

Ethernet RX/TX counters are updated as frames are processed and emitted.
Local loopback UDP/TCP delivery bypasses device packet tokens in this stack,
so those packets are counted when they enter the local protocol processing
path. The counters reflect stack packet processing; they do not prove that a
driver transmitted a frame successfully after consuming a TX token.

The focused regression first failed on the previous kernel at `/proc/net`
lookup; the [baseline QEMU transcript](2026-09-24-proc-net-dev-baseline.log)
has SHA256 `30707dc740c218a2efde49e5a2428f259848cc6e856e3635dc7fa34ee594f31d`.
The test checks path semantics, both headers, 16 decimal fields, growing
loopback RX/TX counts after UDP delivery, and a child network namespace with
fresh `lo` counters and no `eth0`. The dedicated RISC-V QEMU gate attaches a
virtio NIC because the repository's default RISC-V OSDK scheme has no NIC.
It additionally checks `eth0` and its increasing TX bytes and packets after
a UDP send toward the QEMU user-network gateway. The regular network
regression runs the same test without requiring `eth0`.

```sh
tools/docker/run_dev_container.sh --workspace "$(pwd)" -- \
  make run_kernel AUTO_TEST=proc_net_dev TARGET_ARCH=riscv64 SMP=4 \
  FEATURES=riscv_sv39_mode RELEASE=1
```

The patched gate completed and its fail-closed validator accepted the unique
`/proc/net/dev regression passed.` terminal marker. The
[complete QEMU transcript](2026-09-24-proc-net-dev-qemu.log), normalized to LF,
has SHA256 `d1dfc0c0bd45add38086e52edf41ed301ea92f41e1f3b123d0834433725f7782`.
The host Linux test passed in its default mode, and the validator suite passed
26 tests. No full native LMBench run was repeated for this focused metadata
change; disappearance of the old warnings in a new LMBench result remains to
be verified after this branch is combined with the earlier LMBench work.
