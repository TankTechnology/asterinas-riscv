# Megrez Selectable Boot Menu Implementation Plan

**Goal:** Boot RockOS, Basic, Probe, or Desktop from one persistent menu.

**Architecture:** Reuse the existing Stage1, probe implementation, board session,
and immutable artifact identities. A selector module records schema-v3 menu
inputs and generates the exact extlinux bytes. Basic and automatic Probe need
small Stage1 entry-point additions because today's interactive mode requires
Debian and today's probe requires a host request.

**Tech stack:** Python unittest, static RISC-V C, extlinux, cached Docker/QEMU.

Execution is inline in the existing boot worktree, as already authorized.

## 1. Standalone lightweight modes

- [x] Extend `stage1_init.c` with `--root-init=basic` and
  `--root-init=probe-auto`; preserve the current three modes and duplicate-arg
  rejection. Add parser tests to `test_debian_rootfs.py`.
- [x] Basic mounts proc/sysfs, starts the cached BusyBox shell, and
  reports readiness. Include BusyBox only when `STAGE1_BUSYBOX` is specified to
  `build_stage1.sh`; verify ELF architecture and include its required cached
  dynamic libraries. The resulting shared archive is about 4.4 MiB.
- [x] Add `stage1_run_probe_auto` to `stage1_probe.c/.h`, reusing `run_batch`
  for the non-writing boot check and then rebooting. Keep the existing host
  protocol unchanged. Test output and reboot behavior with the C self-test.
- [x] Run `python3 -m unittest tools.riscv.tests.test_debian_rootfs.DebianStage1Tests`
  inside the persistent container. Build the shared lightweight archive there.

## 2. Selector contract and publication

- [x] Create `tools/riscv/megrez_boot_menu.py` and
  `tools/riscv/tests/test_megrez_boot_menu.py`. Parse the actual vendor default
  stanza, preserve its boot directives, reject ambiguity, and render four
  entries with `timeout 100` and RockOS default.
- [x] Record schema-v3 artifact identities, exact vendor bytes, and per-mode
  arguments. Validate shared kernel/DTB and Basic/Probe Stage1; reject unknown
  fields, unsafe paths, bad identity, and mode-inconsistent arguments.
- [x] Add local render/verify and RockOS publication-script entry points.
  Verify installed artifacts before downloading; install only missing files;
  publish a canary first and require matching qualification evidence before
  promoting exact bytes. Keep the vendor config unchanged.
- [x] Run the menu, manifest, probe, and publication unit suites in Docker.

## 3. Frozen generation and QEMU

- [x] Reuse the installed kernel and desktop artifacts after identity checks;
  prepare only the changed lightweight Stage1 and any required prepatched DTB.
- [x] QEMU-boot Basic to a working shell and verify proc/sysfs and reboot;
  QEMU-boot automatic Probe and require completion plus shutdown/reset.
- [x] Record commands, hashes, elapsed time, and failure evidence in target.

## 4. Physical qualification and completion

- [x] Publish the canary through RockOS using the existing serial/root-shell
  maintenance path and network staging. Check size/SHA after sync.
- [x] Test default RockOS and fallback, Basic, and Probe for three successful
  cycles each; test Desktop twice. Reuse artifacts between cycles. Stop and
  diagnose any failure before another trial.
  - Basic, Probe, RockOS/default, and fallback: 3/3 each, candidate 2.
  - Desktop: two visible-window and firmware-recovery passes (284.027 and
    366.184 seconds from menu to readiness). Failed diagnostic runs excluded.
- [x] Promote the qualified menu and verify an uninterrupted software reboot.
  Run `0aea2376e79616bce87486840b2628ef` passed: fresh firmware, persistent
  four-mode menu, no-input default RockOS, and successful serial login.
- [ ] Verify an operator-assisted power cycle separately. The existing
  30-second firmware bootdelay is preserved; the additional menu timeout is
  10 seconds, not a 10-second total boot time. Leave the board in RockOS until
  the operator is available; do not claim a software reboot is a cold start.
- [x] Update the operator README, evidence, and plan status. Review the diff,
  commit scoped changes, and report exact completed versus remaining gates.
  - Native focused gate: 150 tests passed. Rebuilt Stage1 equals the frozen
    candidate hash; refreshed QEMU runs passed (Basic 7.160 s, Probe 5.767 s).
  - Independent commits: `d1dff34b4` (safe U-Boot prompt acquisition),
    `2d6ddd1df` (standalone Stage1 modes and temporary Desktop HOME),
    `e1751fbef` (menu workflow), `3965c21b1` (full-duplex serial checks), and
    `2d2a0c528` (review fix: select the actual vendor default on fallback).
  - Detailed physical evidence and limitations:
    [validation notes](2026-09-12-megrez-menu-validation.md).

Remote `main` remains at `7767494e6`; implementation and validation commits are
local to `codex/megrez-boot-main`. Final cold-start acceptance and the stable-main
handoff remain pending. No network integration source was absorbed here.

## Design clarifications from source inspection

Basic must be independent of Debian, so it uses an explicit `basic` Stage1
mode, not the existing root-handoff `interactive` mode. Probe selected from the
menu runs autonomously; the host-controlled protocol remains available.
The shared Stage1 may also serve Desktop if the existing Debian root-handoff
is sufficient: never duplicate a payload merely to give a mode a different
filename. Desktop rootfs writes under its existing policy are distinct from
deploying or rewriting the partition image.
