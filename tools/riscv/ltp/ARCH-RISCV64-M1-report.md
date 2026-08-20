# RISC-V Architecture LTP M1 Historical Selection Evidence

> **Status:** This is historical suite-selection evidence. It does not
> establish a current baseline for the code in this merge series.

## Historical Outcome

At the historical source commit below, the `arch-riscv64` selection run
completed on Asterinas with four virtual CPUs.
The boot infrastructure passed, all 138 packaged tests produced one ordered
verdict, and the run had no guest crash or timeout.
LTP did not pass as a suite because 37 tests reported FAIL.

```text
suite=arch-riscv64 infrastructure=PASS ltp=FAIL
total=138 pass=81 fail=37 conf=20 crash=0 timeout=0 legacy_fail_total=37
```

## Provenance

- Source branch: `codex/riscv-ltp-integration`
- Source commit: `f7b85470bef964d3012ac755ee0ef0e94f00ff35`
- LTP tag: `20260529`
- LTP commit: `3a64d78f58bdceba93ed321e91215fb969a047ed`
- QEMU profile: `generic-sv39-ltp-smp4`
- SMP count: 4
- Requested tests: 139
- Packaged and executed tests: 138
- Unavailable tests: 1 (`rt_sigtimedwait01`, `missing-binary`)
- Result directory: `target/ltp/results/arch-riscv64-m1-smp4-r2/`
- Prepared boot directory: `target/ltp/qemu/smp4/arch-riscv64-m1-smp4-r2/`

The source commit is not an ancestor of this merge series and is not reachable
from a current `origin` ref. In accordance with the operator guide, these
results justify the reviewed suite selection and closed count contract only;
they must not be reported as verdicts for the merged gate. A current baseline
must be rerun from a reachable post-merge commit and recorded separately.

The pinned cross-container build emitted a warning that
`target/nixos/busybox` was absent. The suite still packaged successfully with
its exact count contract; this environment fact is retained for interpreting
tests that may invoke shell helpers. The current builder rejects a missing
BusyBox artifact, which is another material difference from this historical
run.

## Non-pass Tests

FAIL (37): `brk01`, `brk02`, `clock_gettime03`, `clock_gettime04`, `clone08`,
`clone09`, `clone11`, `clone301`, `clone302`, `clone303`, `getcpu01`, `mmap04`,
`mmap08`, `mmap12`, `mmap13`, `mmap14`, `mmap16`, `mmap18`, `mprotect01`,
`prctl05`, `prctl06`, `prctl09`, `ptrace02`, `ptrace03`, `ptrace11`,
`sched_rr_get_interval01`, `sched_rr_get_interval02`,
`sched_rr_get_interval03`, `sched_setparam05`, `sched_setscheduler02`,
`sched_setscheduler03`, `sched_setscheduler04`, `sched_setaffinity01`,
`sched_setattr01`, `sched_getattr01`, `futex_wait05`, `membarrier01`.

CONF (20): `cacheflush01`, `clone10`, `clone304`, `mmap22`, `prctl07`,
`prctl10`, `ptrace04`, `ptrace07`, `ptrace08`, `ptrace09`, `ptrace10`,
`rt_sigqueueinfo01`, `rt_sigqueueinfo02`, `signal06`, `futex_cmp_requeue01`,
`futex_cmp_requeue02`, `futex_waitv01`, `futex_waitv02`, `futex_waitv03`,
`futex_wake04`.

## Artifact Identities

- Kernel: `661ca5e7de2275c5c48d560bc4932df56a5f5faffc4e5a62f745a18901baf3b0`
- Initramfs: `5c83e014e0a3dcaa48822d05839297ed7f10ce8502d223eb321002c299effaf0`
- Device tree: `f28e5e5e90ef2aa59c3613c580a698ebbd4a31bf2fa6a616c23851aefb9472bb`
- Boot disk: `402c2f38482662c205d655bd572a7e1685f14c8de8c76d97fb8997d6fab630d6`
- Manifest evidence: `731f808429f496f2e926fbb29db51bb6e15fc44252568382be7ab5e1b3288d26`
- Unavailable evidence: `0dd37b6e4490baf3c79e68ac212a3f0625989ecb93d8ffdf776e8f7084c7699d`
- Package identity: `6fdb520c436f41913b47905d1c2b58f6569d6bdb1d2a885a1da8191c43e03ad0`
- Normalized result: `36d10e2bfeb6e3667c4d8737a1b730f36519c43a303723d6c27df5b9910e7af6`

`python3 -m json.tool` accepted `result.json`, and
`sha256sum -c target/ltp/results/arch-riscv64-m1-smp4-r2/SHA256SUMS`
verified all 16 recorded files.
The run-owned `package.json` binds the suite name to the exact initramfs,
manifest, and unavailable-evidence hashes.
The 138 ordered verdicts are identical to the initial pre-hardening run.

## Active Observation Record

- At 0 completed tests, QEMU had booted through U-Boot and entered
  `riscv_boot`; the QEMU, gate, and container processes were all present.
- At 58/138, the current test was `prctl09`; counts were 32 PASS, 21 FAIL,
  5 CONF, with no crash or timeout. QEMU was consuming CPU and making serial
  progress.
- At 134/138, the current test was `futex_wake03`; counts were 79 PASS,
  36 FAIL, 19 CONF, with no crash or timeout. QEMU remained healthy.
- Completion published the protected serial log and normalized result at
  2026-08-20 13:08:23 +08:00. The terminal classification was
  `BOOT_COMPLETED`, and process-group cleanup completed.

## Next Work

The next kernel-fix tranche remains deliberately separate from this gate
change. Diagnose and port the already prioritized cases in this order:
`mmap04`, `clone08`, then the SMP-sensitive `getcpu01`. Routine confirmation
runs should use SMP=4; SMP=1 is reserved for targeted comparison of the
`getcpu01` CPU-affinity behavior.
