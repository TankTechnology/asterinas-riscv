# Megrez Fast Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one bounded command that boots the current Megrez MMC deployment, executes named kernel probes without Debian/systemd/Firefox, publishes compact evidence, and returns the board to a fresh U-Boot prompt.

**Architecture:** Extend the existing Stage1 `/init` with a probe mode and a fixed C probe registry; the host sends one nonce-bound batch over the already shared serial console. A single Python module owns bundle selection, protocol validation, QEMU/physical adapters, compact publication, and recovery while reusing `BoardSession`, `SerialConsole`, `PinnedOutputDirectory`, and `validate_recovery_epoch`.

**Tech Stack:** Safe Rust Asterinas kernel, static C11 Stage1 userspace, Python 3 standard library, `unittest`, QEMU `virt`, Megrez U-Boot/eMMC, persistent project Docker container.

---

## File map

- `tools/riscv/debian/rootfs/stage1_probe.h`: the narrow Stage1-to-probe-agent interface.
- `tools/riscv/debian/rootfs/stage1_probe.c`: fixed probe registry, nonce-bound serial protocol, bounded kernel-log capture, and immediate reboot request.
- `tools/riscv/debian/rootfs/stage1_init.c`: recognize the explicit `--root-init=probe` mode and bypass root-device discovery.
- `tools/riscv/debian/rootfs/build_stage1.sh`: compile the probe agent into the existing single-file Stage1 initramfs.
- `tools/riscv/megrez_probe.py`: bundle contract, CLI, protocol classifier, one-run lifecycle, QEMU adapter, physical adapter, and result publisher.
- `tools/riscv/tests/test_megrez_probe.py`: host contract, lifecycle, protocol, publication, and adapter tests.
- `tools/riscv/tests/test_debian_rootfs.py`: native Stage1 parser/probe-agent/build tests.
- `Makefile`: focused unit, QEMU, and Stage1 artifact targets.
- `tools/riscv/README.md`: one-time deployment selection, normal one-line use, output semantics, and recovery boundary.

### Task 1: Freeze the host bundle and probe contracts

**Files:**
- Create: `tools/riscv/megrez_probe.py`
- Create: `tools/riscv/tests/test_megrez_probe.py`

- [x] **Step 1: Write failing bundle validation tests**

Add `ProbeBundleTests` that constructs a schema-1 JSON bundle containing an exact `DebugPlan.to_dict()`, the by-id serial device, and canonical `kernel`/`initramfs`/`megrez_dtb` MMC paths. Assert that `ProbeBundle.from_bytes()` accepts the canonical form and rejects unknown fields, a non-by-id device, absolute or traversal MMC paths, artifact-name mismatch, bad size/CRC/load address, and a plan digest mismatch.

- [x] **Step 2: Run the bundle tests and observe the missing module**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_megrez_probe.ProbeBundleTests -v
```

Expected: `ImportError` for `tools.riscv.megrez_probe`.

- [x] **Step 3: Implement the immutable bundle types**

Define frozen `MmcArtifact` and `ProbeBundle` dataclasses. `ProbeBundle.from_bytes()` must use exact field sets, reconstruct `DebugPlan` through `DebugPlan.from_bytes()`, require canonical artifact order, compare every MMC identity with the embedded plan, and expose canonical bytes plus `bundle_sha256`. Use this exact top-level shape:

```json
{
  "schema_version": 1,
  "plan": {"schema_version": 2},
  "plan_sha256": "64 lowercase hex digits",
  "device": "/dev/serial/by-id/usb-FTDI_...",
  "mmc_artifacts": [
    {"name": "kernel", "path": "asterinas-commit-crc.Image"},
    {"name": "initramfs", "path": "asterinas-commit-crc-stage1.cpio"},
    {"name": "megrez_dtb", "path": "dtbs/linux-image-version/eswin/eic7700-milkv-megrez.dtb"}
  ]
}
```

- [x] **Step 4: Add failing probe-selection and duration tests**

Assert that `validate_probe_names()` preserves the requested order, accepts only `boot`, `syscall213`, `syscall272`, `ext2-writeback`, and `systemd-compat`, and rejects an empty list, duplicates, unknown names, whitespace, and more than five names. Assert that `validate_session_seconds()` accepts integer values 30 through 300, defaults to 90, and rejects booleans, fractions, and boundary violations.

- [x] **Step 5: Implement the fixed host registry and validators**

Use a frozen `ProbeDefinition(name, timeout_seconds)` registry with limits `boot=5`, `syscall213=5`, `syscall272=5`, `ext2-writeback=15`, and `systemd-compat=10`. Return a tuple of definitions and fail before any serial or filesystem mutation.

- [x] **Step 6: Run focused tests and commit**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_megrez_probe.ProbeBundleTests tools.riscv.tests.test_megrez_probe.ProbeSelectionTests -v
```

Expected: all tests pass.

Commit:

```bash
git add tools/riscv/megrez_probe.py tools/riscv/tests/test_megrez_probe.py
git commit -m "Add Megrez fast probe contracts"
```

### Task 2: Add the minimal Stage1 probe agent

**Files:**
- Create: `tools/riscv/debian/rootfs/stage1_probe.h`
- Create: `tools/riscv/debian/rootfs/stage1_probe.c`
- Modify: `tools/riscv/debian/rootfs/stage1_init.c`
- Modify: `tools/riscv/debian/rootfs/build_stage1.sh`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [x] **Step 1: Write failing parser and native-agent tests**

Extend the Stage1 native self-test list with `root-init-probe`, `root-init-probe-debug-conflict`, and `root-init-probe-duplicate`. Add a native probe harness that feeds this exact request to stdin and checks ordered terminal records:

```text
ASTERINAS_PROBE_RUN v=1 nonce=00112233445566778899aabbccddeeff probes=boot,syscall213,syscall272 shell=0
```

The harness must require one `READY`, ordered `START`/`PASS` records, one `DONE status=pass`, and rejection of a replayed nonce, duplicate names, or malformed input. A second request with `shell=1` must enter the bounded built-in diagnostic console after `DONE`, accept only `help`, `dmesg`, `mounts`, the five registered probe names, and `exit`, and reject every other token without invoking a shell interpreter.

- [x] **Step 2: Run the Stage1 tests and verify the new cases fail**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_debian_rootfs.DebianStage1Tests.test_native_self_test_covers_discovery_and_handoff_failures tools.riscv.tests.test_debian_rootfs.DebianStage1Tests.test_stage1_probe_agent_protocol -v
```

Expected: the probe-mode cases or harness symbols are missing.

- [x] **Step 3: Implement explicit probe-mode parsing**

Add `ROOT_INIT_PROBE` to `enum RootInitMode`. Accept exactly one `--root-init=probe`; reject all `--debug-console=*` arguments in probe mode. In production `main`, configure `/dev/console`, report `DEBIAN_STAGE1_PROGRESS step=start mode=probe`, call `stage1_run_probe_agent()` immediately, and never call `discover_root()` or mount the Debian root.

- [x] **Step 4: Implement the fail-closed probe protocol**

`stage1_run_probe_agent()` must print and flush:

```text
ASTERINAS_PROBE_READY v=1 pid=1
ASTERINAS_PROBE_START v=1 nonce=<nonce> seq=<n> name=<name>
ASTERINAS_PROBE_PASS v=1 nonce=<nonce> seq=<n> name=<name> detail=<safe-token>
ASTERINAS_PROBE_DONE v=1 nonce=<nonce> count=<n> status=pass
ASTERINAS_PROBE_REBOOT_READY v=1 nonce=<nonce>
```

On failure it prints `ASTERINAS_PROBE_FAIL` with `errno=<decimal>` and one safe detail token, emits at most 32 KiB between `ASTERINAS_PROBE_DMESG_BEGIN/END`, then prints `DONE status=fail`. Cap the request at 512 bytes, require exactly 32 lowercase hexadecimal nonce characters, require canonical comma-separated names, and execute no text as a shell command.

- [x] **Step 5: Implement the five fixed probes**

Use direct libc/syscall interfaces only:

- `boot`: prove PID 1, `uname`, and `/dev/console` availability;
- `syscall213`: invoke RISC-V generic syscall 213 (`readahead`) with invalid arguments and pass only when the current kernel returns `ENOSYS`;
- `syscall272`: invoke RISC-V generic syscall 272 (`kcmp`) and pass only when the current kernel returns `ENOSYS`;
- `ext2-writeback`: inspect `/proc/cmdline` to prove `asterinas.mmc_write_partition2` is absent and report the safe `write-gate-closed` result; it must not mount or write partition 2;
- `systemd-compat`: probe `/proc/sys/kernel/random/boot_id`, `/proc/sys/kernel/random/uuid`, and the kernel log-control syscall without starting systemd, reporting the first missing interface.

After `DONE`, print `REBOOT_READY` and accept only `ASTERINAS_PROBE_REBOOT v=1 nonce=<same-nonce>` when `shell=0`. Then call `sync()` and `reboot(RB_AUTOBOOT)`. When `shell=1`, print `ASTERINAS_PROBE_SHELL_READY`, expose only the bounded built-ins listed in Step 1, and leave the fixed kernel reboot timer armed; `exit` requests reboot. A missing or malformed reboot request cannot disable the kernel deadline. If the reboot syscall returns, remain in `pause()` so `asterinas.reboot_after` remains the recovery authority.

- [x] **Step 6: Compile the agent into Stage1 and keep the archive minimal**

Add `stage1_probe.c` to the static compile command and keep `--print-entries` plus the newc archive entries exactly `.` and `init`. Update test compile helpers to link the new source.

- [x] **Step 7: Run Stage1 tests and commit**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_debian_rootfs.DebianStage1Tests -v
```

Expected: all tests pass and the generated archive still lists only `.` and `init`.

Commit:

```bash
git add tools/riscv/debian/rootfs/stage1_probe.h tools/riscv/debian/rootfs/stage1_probe.c tools/riscv/debian/rootfs/stage1_init.c tools/riscv/debian/rootfs/build_stage1.sh tools/riscv/tests/test_debian_rootfs.py
git commit -m "Add a minimal Stage1 kernel probe mode"
```

### Task 3: Classify one complete nonce-bound exchange

**Files:**
- Modify: `tools/riscv/megrez_probe.py`
- Modify: `tools/riscv/tests/test_megrez_probe.py`

- [x] **Step 1: Write failing protocol-classifier tests**

Create transcripts for two probes in one boot. Assert `classify_probe_transcript(transcript, nonce, selected)` returns ordered `ProbeOutcome` values and rejects stale nonces, replayed records, missing `DONE`, reordered sequence numbers, name substitution, duplicate terminal records, unknown protocol lines, unsafe detail fields, count mismatch, and a `PASS` after any `FAIL`.

- [x] **Step 2: Run the classifier tests and verify failure**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_megrez_probe.ProbeProtocolTests -v
```

Expected: classifier symbols are missing.

- [x] **Step 3: Implement the strict line classifier**

Define frozen `ProbeOutcome(sequence, name, passed, errno, detail)` and `ProbeExchange(outcomes, passed, dmesg)`. Parse only exact anchored regular expressions, require one nonce throughout, enforce `START` immediately followed by one terminal record for the same sequence/name, require the selected count, and allow dmesg frames only after a failed outcome and before `DONE`.

- [x] **Step 4: Add and test request encoding**

`encode_probe_request(nonce, selected, shell=False)` must return one ASCII line ending in `\n`, fit within 512 bytes, contain the exact `shell=0|1` selector, and contain no shell metacharacter or user-controlled free text. Test exact byte equality for `boot,syscall213` in both modes.

- [x] **Step 5: Run focused tests and commit**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_megrez_probe.ProbeProtocolTests -v
```

Expected: all tests pass.

Commit:

```bash
git add tools/riscv/megrez_probe.py tools/riscv/tests/test_megrez_probe.py
git commit -m "Validate the Megrez fast probe protocol"
```

### Task 4: Implement the bounded one-run lifecycle and compact publisher

**Files:**
- Modify: `tools/riscv/megrez_probe.py`
- Modify: `tools/riscv/tests/test_megrez_probe.py`

- [ ] **Step 1: Write failing lifecycle tests with fake operations**

Define a `ProbeOperations` protocol with `open`, `ensure_artifacts`, `boot`, `exchange`, `request_reboot`, `await_recovery`, and `close`. Test that `run_probe()` invalidates stale `result.json` first, performs one artifact load and one boot for multiple probes, never includes network/systemd/partition-write/Firefox arguments, requests immediate reboot after terminal output, always attempts bounded recovery after guest start, and returns `manual-reset-required` when no fresh U-Boot epoch appears.

- [ ] **Step 2: Run lifecycle tests and verify failure**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_megrez_probe.ProbeLifecycleTests -v
```

Expected: lifecycle symbols are missing.

- [ ] **Step 3: Implement probe bootargs and lifecycle**

Derive bootargs from the embedded plan only to retain its board/kernel parameters. Remove every `console=`, `loglevel=`, `asterinas.klog_capture=`, `asterinas.reboot_after=`, `asterinas.mmc_write_partition2`, `asterinas.net=`, `asterinas.neighbor=`, `systemd.*`, and existing Stage1 argument. Append exactly:

```text
console=ttyS0 loglevel=info asterinas.klog_capture=info init=/init asterinas.reboot_after=<30..300> -- --root-init=probe
```

Use one monotonic absolute guest deadline; every operation receives only its remaining budget. Recovery receives a separate fixed 30-second window.

For `--shell`, require a local TTY, forward newline-delimited built-in names without terminal escape translation, stop forwarding at the same fixed guest deadline, and never renew `asterinas.reboot_after`. Reject `--shell` in noninteractive automation before touching the board.

- [ ] **Step 4: Implement private result publication**

Use `PinnedOutputDirectory` and mode `0600`. Invalidate `result.json`, `serial-summary.log`, `failure.dmesg.log`, and `sha256sums.txt` before opening the bundle or board. Publish `serial-summary.log` with protocol lines plus at most 4 KiB surrounding context; publish `failure.dmesg.log` only on failure; publish `sha256sums.txt` over retained files; publish canonical `result.json` last with schema, bundle/plan hashes, selected probes, outcomes, elapsed seconds, recovery state, and terminal reason.

- [ ] **Step 5: Add publisher fault-injection tests**

Assert a mid-publication exception leaves no `result.json`, a stale successful result cannot survive a later invalid bundle, output files reject symlink replacement, each retained hash verifies, and success has no `failure.dmesg.log`.

- [ ] **Step 6: Run lifecycle and publisher tests and commit**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_megrez_probe.ProbeLifecycleTests tools.riscv.tests.test_megrez_probe.ProbePublisherTests -v
```

Expected: all tests pass.

Commit:

```bash
git add tools/riscv/megrez_probe.py tools/riscv/tests/test_megrez_probe.py
git commit -m "Add the bounded Megrez probe lifecycle"
```

### Task 5: Reuse the physical board session and add the simple CLI

**Files:**
- Modify: `tools/riscv/megrez_probe.py`
- Modify: `tools/riscv/tests/test_megrez_probe.py`
- Modify: `Makefile`

- [ ] **Step 1: Write failing physical-adapter and CLI tests**

Mock `BoardSession` and `SerialConsole`. Require exactly three MMC `ext4load` plus CRC checks, no host artifact transfer, no framebuffer/USB/network/U-Boot-environment commands, one `booti`, one encoded probe request after `READY`, one immediate reboot request, and `validate_recovery_epoch()` over a newly observed OpenSBI/U-Boot/prompt sequence. Test the normal CLI with only probe names and the one-time `configure` command that atomically writes `target/megrez-probe/current.json`.

- [ ] **Step 2: Run adapter/CLI tests and verify failure**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_megrez_probe.PhysicalProbeOperationsTests tools.riscv.tests.test_megrez_probe.ProbeCliTests -v
```

Expected: adapter or CLI entry points are missing.

- [ ] **Step 3: Implement `PhysicalProbeOperations` using existing primitives**

Open the by-id device with `open_serial`, acquire the existing nonblocking exclusive serial lock, and wrap it in `BoardSession.from_fd(confirm=False)`. Wake and require U-Boot, load the three bundle paths with `BoardSession.load_artifact`, prepare only `mmc dev 1`, `mmc rescan`, `fdt addr`, `initrd_size`, and safe chunked `setenv bootargs`, then `booti`. Use `SerialConsole` for the request and exchange. Do not call `RealPhysicalGraphicsOperations.boot()` because its framebuffer and USB preparation is intentionally outside this path.

- [ ] **Step 4: Implement the normal and configure CLI paths**

Normal defaults must be:

```text
bundle=target/megrez-probe/current.json
output=target/megrez-probe/latest
session-seconds=90
recovery-seconds=30
```

`configure` takes `--plan`, `--device`, and the three `--mmc-*` paths, validates all inputs, atomically replaces `current.json`, and prints its bundle hash. Normal use accepts only probe names plus optional `--bundle`, `--output-directory`, bounded duration flags, and `--shell`. Unit tests require `--shell --session-seconds=29` and `301` to fail before the adapter is constructed, and require a 180-second shell request to preserve one immutable deadline through boot, probes, console input, and recovery.

- [ ] **Step 5: Add the focused Make target and run tests**

Add `test_riscv_megrez_probe_unit` running the new test module plus the Stage1 probe tests. Run:

```bash
make test_riscv_megrez_probe_unit
```

Expected: all focused tests pass.

- [ ] **Step 6: Commit**

```bash
git add Makefile tools/riscv/megrez_probe.py tools/riscv/tests/test_megrez_probe.py
git commit -m "Add the one-command Megrez probe runner"
```

### Task 6: Prove the guest in QEMU

**Files:**
- Modify: `tools/riscv/megrez_probe.py`
- Modify: `tools/riscv/tests/test_megrez_probe.py`
- Modify: `Makefile`

- [ ] **Step 1: Add a failing QEMU command-construction test**

Require `qemu-system-riscv64 -machine virt -m 2G -smp 4 -nographic -no-reboot`, the selected Asterinas kernel and Stage1 initramfs, and the same probe bootargs/protocol classifier used by the physical adapter. Reject paths that are not pinned regular files.

- [ ] **Step 2: Implement `--qemu` as an adapter, not a second lifecycle**

Start QEMU in its own process group with stdin/stdout on a PTY, wait for `ASTERINAS_PROBE_READY`, run the same exchange, and require process exit after the guest reboot. A `--qemu-deadline-only` test variant sends no request and proves `asterinas.reboot_after=30` terminates the guest within 45 seconds.

- [ ] **Step 3: Build through the persistent container**

Run:

```bash
tools/docker/run_dev_container.sh -- tools/riscv/debian/rootfs/build_stage1.sh target/megrez-probe/build/initramfs.cpio
tools/docker/run_dev_container.sh -- make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
```

Expected: cached container reuse, one Stage1 initramfs, and one RISC-V kernel image; no image/container deletion or Cargo OSDK download.

- [ ] **Step 4: Run the two-probe and deadline QEMU gates**

Run the normal gate with `boot syscall213`, then the 30-second deadline-only variant. Expected: normal `DONE status=pass` in one boot; both runs terminate through guest reboot; no Debian root disk is attached.

- [ ] **Step 5: Add the reproducible QEMU Make target and commit**

Add `test_riscv_megrez_probe_qemu` with explicit kernel/initramfs inputs and commit:

```bash
git add Makefile tools/riscv/megrez_probe.py tools/riscv/tests/test_megrez_probe.py
git commit -m "Add the Megrez probe QEMU gate"
```

### Task 7: Select the new deployment and run one physical probe

**Files:**
- Modify: `tools/riscv/README.md`
- Generated, not committed: `target/megrez-probe/current.json`
- Generated, not committed: `target/megrez-probe/physical/result.json`

- [ ] **Step 1: Build and identify the exact Stage1 artifact**

Build in the persistent container, compute size/SHA-256/CRC32, and create a new immutable debug plan that retains the current kernel and DTB identities but names the rebuilt Stage1 artifact. Confirm the plan contains the 90-second probe recovery parameter only in the derived bootargs, not by weakening the browser-plan validation contract.

- [ ] **Step 2: Place the versioned Stage1 file on partition 1 using RockOS**

Boot the existing RockOS installation, transfer the small versioned Stage1 file over the already configured network path, write it to `/boot` with a temporary name, `fsync`, verify size and SHA-256, atomically rename it, and reboot normally to fresh U-Boot. Do not modify partition 2 or erase any existing boot artifact.

- [ ] **Step 3: Create the current bundle once**

Run `python3 -m tools.riscv.megrez_probe configure` with the new plan, the known by-id FTDI device, current versioned kernel/DTB paths, and new Stage1 path. Re-open `current.json`, verify its canonical hash, and confirm permissions are private.

- [ ] **Step 4: Execute the physical acceptance run**

Run:

```bash
python3 -m tools.riscv.megrez_probe boot syscall213 --output-directory target/megrez-probe/physical
```

Expected: three MMC loads with byte-count/CRC evidence, one Asterinas boot, two ordered passing probes, immediate reboot, fresh OpenSBI/U-Boot/prompt evidence, `passed=true`, and no RockOS/Firefox/systemd/network/partition-2 activity during the probe run.

- [ ] **Step 5: Verify the retained evidence**

Recompute every line in `sha256sums.txt`, confirm `result.json` was published last and is mode `0600`, inspect the bounded serial summary, and record elapsed time versus the previous 175–220 second Firefox readiness baseline.

- [ ] **Step 6: Document the one-line loop and commit**

Document `configure` as deployment-only and `python3 -m tools.riscv.megrez_probe <names>` as the routine loop. State that timer/SBI hard locks still require manual reset and that RockOS/SHA attestation remains the release workflow.

Commit:

```bash
git add tools/riscv/README.md
git commit -m "Document the Megrez fast probe loop"
```

### Task 8: Full verification, review, and fast-forward publication

**Files:**
- Modify only files required by review findings.

- [ ] **Step 1: Run formatting and static checks**

Run `ruff format --check` and `ruff check` on changed Python files, `python3 -m py_compile` on them, `bash -n` on changed shell scripts, the Stage1 `-Wall -Wextra -Werror` build, and `git diff --check`. Expected: zero errors.

- [ ] **Step 2: Run the complete relevant regression set**

Run the new unit/QEMU gates, all Debian rootfs tests, existing Megrez debug/physical-graphics/boot-stability tests, Docker launcher tests, and the RISC-V kernel build in the persistent container. Expected: all pass without recreating the container or downloading `cargo-osdk`.

- [ ] **Step 3: Review against Asterinas persona guidelines**

Review the diff for maintainability, development correctness, security, hardware contract compliance, and documentation. Resolve every Critical or Important finding, rerun the focused test, and retain the Markdown review artifact required by `aster-code-review`.

- [ ] **Step 4: Confirm repository and remote safety**

Require a clean worktree, inspect every commit being published, run `git fetch origin`, and require `git merge-base --is-ancestor origin/main HEAD`. If `origin/main` advanced, integrate it without force and rerun the relevant gates.

- [ ] **Step 5: Push only a verified fast-forward**

Run:

```bash
git push origin HEAD:main
```

Expected: a non-forced fast-forward of `asterinas-riscv/main`. Re-fetch and require `git rev-parse origin/main` equals `git rev-parse HEAD` before reporting completion.
