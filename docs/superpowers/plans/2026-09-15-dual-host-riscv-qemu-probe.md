# Dual-host RISC-V QEMU Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Run one identical lightweight Asterinas probe in the persistent developer container and on RockOS under QEMU/TCG.

**Architecture:** A host-side Python command pins and hashes two artifacts, runs the existing Stage1 serial probe protocol through a PTY, and publishes separate evidence for each QEMU host. The developer QEMU uses the already-running persistent container; RockOS uses SSH, its installed QEMU, and hash-verified `/tmp` cache files. Both processes have remote-side `timeout` protection.

**Tech Stack:** Python 3.12 stdlib, existing `megrez_probe` protocol/`gate_runtime` process primitives, Docker exec, OpenSSH, QEMU RISC-V `virt`, unittest.

---

## File map

- Create `tools/riscv/dual_host_qemu_probe.py`: artifact identity, bounded transports, fixed Stage1 exchange, CLI, and evidence.
- Create `tools/riscv/tests/test_dual_host_qemu_probe.py`: red/green contract tests and failure-path process tests.
- Modify `Makefile`: one dual-host probe target with explicit artifact and RockOS SSH inputs.
- Modify `tools/riscv/README.md`: short routine command and virtual-vs-physical boundary.
- Modify `docs/superpowers/specs/2026-09-15-dual-host-riscv-qemu-probe-design.md`: clarify that developer QEMU is inside the persistent container, because the host has no QEMU binary.

### Task 1: Artifact and command contracts

- [x] **Step 1: Write the failing tests** in `tools/riscv/tests/test_dual_host_qemu_probe.py`: assert `read_identity` returns the byte count and SHA-256 of a regular file, rejects a symlink and a >64-MiB file; assert `qemu_bootargs()` selects only Stage1 probe mode; assert `developer_argv()` and `rockos_argv()` contain `-nic none`, `-no-reboot`, and a bounded `timeout`, with the latter using TCG and `nice`.
- [x] **Step 2: Verify red.** Run `tools/docker/run_dev_container.sh --offline -- python3 -m unittest tools.riscv.tests.test_dual_host_qemu_probe -v`; expect missing module/functions, not a test setup error.
- [x] **Step 3: Implement** the tested helpers in `dual_host_qemu_probe.py`: `read_identity(path: Path) -> ArtifactIdentity` using `os.open(..., O_NOFOLLOW)` and `fstat`, incremental SHA-256; `qemu_bootargs() -> str` with `console=ttyS0 loglevel=info asterinas.klog_capture=info init=/init asterinas.reboot_after=180 -- --root-init=probe`; `developer_argv(container, kernel, initramfs, seconds)` wrapping `qemu_probe_argv` in `docker exec -it ... timeout -k 5`; `rockos_argv(target, kernel, initramfs, seconds)` wrapping the same QEMU arguments in `ssh -tt ... timeout -k 5 nice -n 10`, with `-accel tcg` and shell-quoted remote paths. Validate container/target and seconds before rendering.
- [x] **Step 4: Verify green** with the same unittest command and run existing `make test_riscv_megrez_probe_unit` in the persistent container.
- [x] **Step 5: Commit** the helpers and tests as `Add bounded dual-host QEMU command contracts`.

### Task 2: RockOS immutable staging

- [x] **Step 1: Write failing tests** for `remote_identity` and `stage_artifact`: a correctly hashed cached file skips `scp`; a missing file is copied to a unique `.part` name then checked and promoted; an existing wrong-hash file fails closed without overwrite; a bad partial copy is removed by its exact path only. Use a fake transport that records real `ssh`/`scp` command arguments and returns explicit results.
- [x] **Step 2: Verify red** with the focused unittest command.
- [x] **Step 3: Implement** `remote_identity(target, path, deadline) -> ArtifactIdentity | None` using `test -f`, `test ! -L`, `stat -c %s`, and `sha256sum`, with a subprocess deadline and strict output parser; implement `stage_artifact` in `/tmp/asterinas-qemu-probe/<paired-sha-prefix>/` by rechecking cached bytes, copying once through `scp`, verifying the partial, atomically promoting it, then rechecking final bytes. Never access another remote path.
- [x] **Step 4: Verify green** and run the full focused suite.
- [x] **Step 5: Commit** as `Cache verified probe artifacts on RockOS`.

### Task 3: Fixed probe exchange and evidence

- [x] **Step 1: Write failing tests** for a synthetic PTY guest: `run_session` waits for `ASTERINAS_PROBE_READY`, sends a nonce-bound `boot,syscall213` request, rejects reordered/stale/failure markers via `classify_probe_transcript`, sends `ASTERINAS_PROBE_REBOOT`, requires QEMU exit zero, and closes the whole process group on timeout. Test separate JSON/serial outputs for each host.
- [x] **Step 2: Verify red** with the focused unittest command.
- [x] **Step 3: Implement** `run_session(argv, nonce, seconds) -> SessionEvidence` using `launch_process`, `SerialConsole`, and a raw `os.openpty`; use existing `encode_probe_request`, `validate_probe_names`, and `classify_probe_transcript`. Catch failures per host, retain the capped transcript and last phase, and always terminate the owned process group. Publish each host's JSON and serial bytes atomically in a pinned output directory.
- [x] **Step 4: Verify green** and run the existing probe suite.
- [x] **Step 5: Commit** as `Record independent developer and RockOS probe evidence`.

### Task 4: CLI, documentation, and real dual-host run

- [x] **Step 1: Write failing CLI tests** for required explicit artifact paths, valid SSH target, output isolation, and nonzero exit when either host fails.
- [x] **Step 2: Verify red** with the focused unittest command.
- [x] **Step 3: Implement** the CLI and `Makefile` target so one command runs developer-container QEMU followed by RockOS QEMU on the same identities; update the README and the spec's developer-container detail.
- [x] **Step 4: Verify green** in the persistent container; build Stage1 and kernel there only if the selected cached pair is incompatible, then run one small developer probe, one RockOS probe, and a second RockOS run to verify no retransfers. Record both SHA-256s and RockOS `uname -r` before/after.
- [x] **Step 5: Verify** `git diff --check`, focused and existing probe suites, real structured results, no remote QEMU remains; commit documentation/CLI. Do not push or alter `main` without a separate integration decision.

## Self-review

The tasks cover both virtual hosts, identity/cache reuse, bounded lifetime, protocol proof, evidence separation, unit tests, and the real run. There is no KVM, Firefox, partition, or boot-path task. Developer-container execution is an implementation detail within the approved developer-host goal.
