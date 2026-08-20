# RISC-V Architecture LTP M1 Baseline

## Outcome

The first closed `arch-riscv64` baseline completed on Asterinas with four
virtual CPUs. The boot infrastructure passed, all 138 packaged tests produced
one ordered verdict, and the run had no guest crash or timeout. LTP did not
pass as a suite because 37 tests reported FAIL.

```text
suite=arch-riscv64 infrastructure=PASS ltp=FAIL
total=138 pass=81 fail=37 conf=20 crash=0 timeout=0 legacy_fail_total=37
```

## Provenance

- Source branch: `codex/riscv-ltp-integration`
- Source commit: `27148cc27f82c09c81c5239f4d9c30f5fff8ec37`
- LTP tag: `20260529`
- LTP commit: `3a64d78f58bdceba93ed321e91215fb969a047ed`
- QEMU profile: `generic-sv39-ltp-smp4`
- SMP count: 4
- Requested tests: 139
- Packaged and executed tests: 138
- Unavailable tests: 1 (`rt_sigtimedwait01`, `missing-binary`)
- Result directory: `target/ltp/results/arch-riscv64-m1-smp4/`
- Prepared boot directory: `target/ltp/qemu/smp4/arch-riscv64-m1-smp4/`

The pinned cross-container build emitted a warning that
`target/nixos/busybox` was absent. The suite still packaged successfully with
its exact count contract; this environment fact is retained for interpreting
tests that may invoke shell helpers.

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
- Initramfs: `e47e549809dd9c494e6718fd9fa17adf0b3d12de9f4752a7e9f798e8381537b5`
- Device tree: `d58cc10a3688264d36310c0378e58c01f79bd0cd6ff292fd8cbd0e9a688e3039`
- Boot disk: `789be7c4f4e13a7a587c201d7029cd417b32f092bcfd13a6b2ecccbc11c7f0c0`
- Manifest evidence: `731f808429f496f2e926fbb29db51bb6e15fc44252568382be7ab5e1b3288d26`
- Unavailable evidence: `0dd37b6e4490baf3c79e68ac212a3f0625989ecb93d8ffdf776e8f7084c7699d`
- Normalized result: `d336148cc29757b86bac7e984424ccf2f8ae62c0f6c82b7eae431fd0b64b0a03`

`python3 -m json.tool` accepted `result.json`, and
`sha256sum -c target/ltp/results/arch-riscv64-m1-smp4/SHA256SUMS` verified all
14 recorded files.

## Active Observation Record

- At 0 completed tests, QEMU had booted through U-Boot and entered
  `riscv_boot`; the QEMU, gate, and container processes were all present.
- At 49/138, the current test was `personality01`; counts were 26 PASS,
  19 FAIL, 4 CONF, with no crash or timeout. QEMU was consuming CPU and making
  serial progress.
- At 128/138, the current test was `futex_wait05`; counts were 77 PASS,
  35 FAIL, 16 CONF, with no crash or timeout. QEMU remained healthy.
- Completion published the protected serial log and normalized result at
  2026-08-20 12:45:57 +08:00. The terminal classification was
  `BOOT_COMPLETED`, and process-group cleanup completed.

## Next Work

The next kernel-fix tranche remains deliberately separate from this gate
change. Diagnose and port the already prioritized cases in this order:
`mmap04`, `clone08`, then the SMP-sensitive `getcpu01`. Routine confirmation
runs should use SMP=4; SMP=1 is reserved for targeted comparison of the
`getcpu01` CPU-affinity behavior.
