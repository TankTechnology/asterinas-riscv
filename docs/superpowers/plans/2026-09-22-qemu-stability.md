# QEMU stability investigation

The user approved QEMU-first stability investigation on the developer host and
the running RockOS board. Physical Asterinas boot/debugging is deferred until
the next workday. This is an experiment plan, not a claim of a diagnosed fix.

## Fixed baseline

- Source: clean `.worktrees/megrez-playback-v2`, commit `3aa196979`.
- Production release Image SHA-256:
  `54d8b57b7a5d97883fb353328c04702ec088adbf2c8bce61d52e29948b79b794`.
- Keep the dirty main workspace's diagnostic changes out of this baseline.
- Standalone probes: QEMU virt, generic SV39 CPU, TCG, four vCPUs, 2 GiB
  RAM, no disks/network. The additional Firefox gate uses scratch image files
  and QEMU user-mode networking on the developer host.
- RockOS: ordinary `debian` user, nice 10, one guest at a time, bounded timeout.
  Do not reboot RockOS or change physical boot selectors/partitions.

## Ordered experiments

- [x] Verify RockOS SSH, boot identity, memory and running guest inventory.
- [x] Run existing nonce-bound boot/syscall probe on RockOS with frozen release
  Image and existing Stage1; require protocol pass and clean guest exit.
- [x] Run all OSTD ktests in the existing isolated persistent container,
  release/SMP=4. Inspect guest counts and completion, not only host exit.
- [x] Build a disposable static initramfs containing unchanged existing
  `pthread_cond_handoff`, `cpu_affinity`, `inheritance`, `group_stop_futex`,
  and `process_pipe_available` tests. A small PID1 supervisor prints per-child
  status and a nonce-bound terminal result. Keep artifacts under a fresh
  `target/qemu-stability-20260922/` directory and record source hashes.
- [x] Run conditional-variable handoffs concurrently and repeatedly; run
  migration, pipe and signal/futex cases separately for failure attribution.
  Preserve serial output and require exact expected child completion counts.
- [x] Cross-check the same artifact pair on RockOS. A timeout is a failure to
  complete, not proof of kernel deadlock. Preserve logs before any rerun.
- [x] Audit the exact baseline's switch ownership wait/release and serial
  receive/wakeup paths. Distinguish observed failures from hypotheses.
- [x] No workload failure reproduced; this conditional step was not needed.
  For a reproducible failure, use the smallest controlled follow-up;
  capture CPU state if progress stalls before changing kernel behavior.
- [x] Save results, commands, limitations, and next-day physical validation
  priorities. Verify RockOS boot identity and SSH after remote experiments.

## Acceptance boundary

Passing probes cover the executed workloads only. They do not establish
Firefox/Bilibili stability, hardware UART/USB/MMC correctness, or a performance
comparison between host architectures. Do not increase test deadlines merely
to turn a failure into a pass; label an explicit timing follow-up separately.

## Completed additions

- UART component ktests: 80 passed.
- Serial command round trips under concurrent handoff load on both hosts.
- Permanent migration/usercopy-fault/TLS regression registered in both runners.
- Release Firefox QEMU desktop gate: three interaction cycles passed.
- Full results and limits: `docs/porting/evidence/2026-09-22-qemu-stability.md`.
