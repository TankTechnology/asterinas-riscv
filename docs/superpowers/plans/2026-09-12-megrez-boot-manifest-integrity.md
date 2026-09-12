# Megrez Boot Manifest Integrity Implementation Plan

> **For Codex:** Execute this plan task by task with `superpowers:executing-plans` and use test-driven development for every behavior change.

**Goal:** Make a reset-safe Megrez boot generation indivisible: the fast-probe bundle, persistent extlinux entry, kernel, Stage1 initramfs, and DTB must agree before `booti`, and a stale extlinux entry must fail closed with an actionable result.

**Architecture:** Add a small, pure `megrez_boot_manifest` module that parses and renders the one supported extlinux entry and binds its three paths to the exact `DebugPlan` identities. Upgrade `ProbeBundle` to schema 2 by embedding the extlinux path and size/SHA-256/CRC32 identity. The physical adapter verifies the extlinux file from MMC before the three boot artifacts; RockOS remains the only publisher and installs immutable artifact names before atomically replacing `/boot/extlinux/asterinas.conf` last.

**Tech Stack:** Python 3 dataclasses and `unittest`, existing `DebugPlan`, `BoardSession`, `PinnedOutputDirectory`, U-Boot `ext4load`/`crc32`, persistent Asterinas Docker container, RockOS maintenance shell.

---

### Task 1: Specify and parse one immutable extlinux generation

**Files:**
- Create: `tools/riscv/megrez_boot_manifest.py`
- Create: `tools/riscv/tests/test_megrez_boot_manifest.py`

- [x] Add a regression fixture for `MEGREZ-BOOT-MANIFEST-001` whose `linux` and `initrd` paths name the absent `asterinas-sv48-fe1dcfdf7.booti` and `initramfs-full-712208ba4.cpio`; require validation to report the missing kernel before any board operation.
- [x] Add failing tests for missing/duplicate `default`, `label`, `linux`, `initrd`, `fdt`, and `append` directives; path traversal; unexpected labels; mutable artifact names; and a path that does not contain the first 12 hexadecimal digits of the corresponding plan artifact SHA-256.
- [x] Implement `ExtlinuxGeneration` with strict UTF-8 parsing, canonical rendering, bounded input, exact directive cardinality, safe absolute paths, and `validate_against_plan(plan)`. Keep comments and generic extlinux features out of the accepted generated subset so that the audited bytes are deterministic.
- [x] Add staged-directory validation using descriptor-safe regular-file reads. Require each referenced file to match the plan size, SHA-256, and CRC32; reject symlinks and files that change while being read.
- [x] Run `python3 -m unittest -v tools.riscv.tests.test_megrez_boot_manifest` and commit only the new module and tests.

### Task 2: Bind `current.json` to the exact persistent configuration

**Files:**
- Modify: `tools/riscv/megrez_probe.py`
- Modify: `tools/riscv/tests/test_megrez_probe.py`

- [x] Add failing schema-v2 tests for an embedded extlinux identity with exact `path`, `size`, `sha256`, and `crc32`; reject legacy schema 1 on physical execution, digest mismatches, unsafe config paths, stale entry paths, and mutable artifact names.
- [x] Change `configure` to require `--extlinux-config` and `--mmc-extlinux`. Parse the config through `ExtlinuxGeneration`, require it to reference the exact three `--mmc-*` paths and plan identities, then atomically publish schema-v2 `current.json`.
- [x] Preserve QEMU fixture usability by allowing schema 1 only inside the explicit `--qemu` adapter. The ordinary physical command must load schema 2 before constructing or opening `PhysicalProbeOperations`.
- [x] Add the generation/config digest to the retained physical `result.json`, so a passing run identifies the persistent reset path it audited.
- [x] Run `python3 -m unittest -v tools.riscv.tests.test_megrez_probe` and commit only the probe/module test changes.

### Task 3: Fail before `booti` when persistent MMC state is stale

**Files:**
- Modify: `tools/riscv/megrez_probe.py`
- Modify: `tools/riscv/tests/test_megrez_probe.py`

- [x] Add a failing physical-adapter test that expects the extlinux configuration to be loaded and CRC-checked first, then the kernel, initramfs, and DTB; any config error must prevent all artifact loads and `booti`.
- [x] Extend `PhysicalProbeOperations.ensure_artifacts()` to validate `/extlinux/asterinas.conf` at a dedicated non-overlapping load address before validating the three plan artifacts. Use the bundle's exact size/CRC and keep one monotonic deadline across all four reads.
- [x] Assert that the generated command stream contains no `saveenv`, filesystem write, YMODEM, network, partition-2, Firefox, or fallback boot action. A mismatch reports `manual-reset-required` only if the board state is no longer provably at U-Boot; otherwise it leaves the prompt live.
- [x] Run the focused physical-operation tests, then the full `test_megrez_probe` module, and commit the adapter change.

### Task 4: Generate a rollback-safe RockOS publication transaction

**Files:**
- Modify: `tools/riscv/megrez_boot_manifest.py`
- Modify: `tools/riscv/tests/test_megrez_boot_manifest.py`
- Modify: `tools/riscv/megrez_rockos_attestation.py`
- Modify: `tools/riscv/tests/test_megrez_rockos_attestation.py`

- [x] Add failing tests for a publication command sequence that writes only new immutable artifact paths, verifies source and installed size/SHA-256, calls `sync`, writes the configuration to a same-directory temporary name, verifies it, atomically renames it to `/boot/extlinux/asterinas.conf` last, and calls `sync` again.
- [x] Reject an existing destination with different bytes, insufficient space, wrong `/boot` backing partition, shell metacharacters, overwritten artifact names, config publication before artifact verification, deletion of old files, or any partition-2 path.
- [x] Reuse the existing RockOS serial login/recovery lifecycle and credential handling. Add an explicit publication action; keep measurement as a separate post-reboot attestation and never place credentials in argv, environment, evidence JSON, or serial logs.
- [x] Publish a canonical generation manifest plus redacted transaction transcript atomically on the host. Failure must preserve the old config and old artifacts and must still attempt normal recovery to U-Boot.
- [x] Run both boot-manifest and RockOS test modules and commit the publication path.

### Task 5: Add the automated regression gate and operator workflow

**Files:**
- Modify: `Makefile`
- Modify: `tools/riscv/README.md`
- Modify: `docs/superpowers/specs/2026-09-12-megrez-boot-manifest-integrity-design.md` only if implementation findings refine the approved contract

- [x] Add `test_riscv_megrez_boot_manifest_unit` and include it in the existing RISC-V host-test group without rebuilding an image or downloading dependencies.
- [x] Document the deployment-only sequence: create immutable SHA-prefixed names, render and locally validate the generation, publish once through RockOS, attest after reboot, and run `python3 -m tools.riscv.megrez_probe boot`. Document `MEGREZ-BOOT-MANIFEST-001` and the exact fail-closed message.
- [x] Add a QEMU regression that feeds the stale fixture and proves rejection before QEMU starts, then runs a valid schema-v2 bundle through the existing lightweight probe/recovery adapter.
- [x] Run Python format/static checks available in the persistent container, the two new unit modules, the existing probe/RockOS/boot-stability tests, `git diff --check`, and the existing QEMU probe gate. Commit only the Makefile/docs/test integration.

QEMU evidence on 2026-09-12: bundle `1a44c248e8160522...` and extlinux
`aaf76a1db7c696bd...` passed `boot syscall213` in 5.330 seconds and passed the
30-second unattended recovery gate in 34.466 seconds.  An initial run using
the network-only Stage1 failed closed at its expected TCP prerequisite and was
not treated as manifest evidence.

Physical publication incident `MEGREZ-ROCKOS-PUBLISH-001` on 2026-09-12: the
first RockOS transaction tried to install the immutable kernel as the ordinary
`debian` user and failed with `Permission denied` before changing `/boot`.
The transaction latch emitted failure for every remaining item, did not replace
`asterinas.conf`, rebooted normally, and returned the board to U-Boot.  The
first regression fix authenticated `sudo` before transaction evidence began,
but a second fail-closed attempt proved that this RockOS policy discards the
timestamp before the next `sudo -n` command.  The final fix enters one
password-authenticated root shell before evidence capture, assigns a unique
root prompt, keeps privileged mutations non-interactive, and reboots directly
from that shell.  Configuration download, verification, same-directory
staging, and the final atomic rename remain separate and config-last.  Focused
tests also enforce the root-shell transition and serial command-size limit.

### Task 6: Repair partition 1 once and prove one physical epoch

**Files:**
- Generated, not committed: `target/megrez-boot-manifest/<generation>/`
- Generated, not committed: `target/megrez-probe/current.json`
- Generated, not committed: `target/megrez-probe/physical/result.json`

- [x] Freeze the current SV39 kernel, lightweight Stage1, and Megrez DTB identities. Build only if a frozen source is absent; use `tools/docker/run_dev_container.sh` and never delete the persistent container or its caches.
- [x] Run all software gates before opening `/dev/ttyUSB0`. Confirm exclusive serial ownership and a live U-Boot prompt, then boot RockOS only for the bounded publication attempts. Do not modify partition 2, persistent U-Boot environment, or any old generation.
- [x] Reboot normally, require a fresh OpenSBI/U-Boot epoch, attest the four persistent files, and create schema-v2 `current.json` from the attested bytes.
- [x] Run exactly one lightweight physical probe. Require extlinux plus three artifact size/CRC checks, terminal `PASS`, Asterinas software reboot, a fresh firmware epoch, and interruption of the next autoboot so the terminal state is U-Boot.
- [x] Verify evidence hashes and confirm the transcript contains no serial upload, partition write during the probe, RockOS activity during the probe, Firefox, fallback boot, or `saveenv`. If any condition fails, retain evidence and do not claim the reset path is repaired.

Physical evidence on 2026-09-12: publication nonce `9454f6a63353...`
installed kernel `485b9079c204...`, Stage1 `d12e5ec8ca4c...`, Megrez DTB
`02a8d43d581b...`, and extlinux `aaf76a1db7c...`, then recovered normally
to U-Boot.  The schema-v2 bundle is `cbc1d0955851...`.  Its single `boot`
probe loaded and CRC-checked extlinux first (`beb20b5f`), then kernel
(`24f33f77`), initramfs (`c0dc29d2`), and DTB (`4afcb20e`); it emitted
terminal `PASS`, software-rebooted, validated a fresh OpenSBI/U-Boot/prompt
epoch, and finished in 41.98 seconds with `passed=true` and `recovered=true`.
Both retained evidence files passed `sha256sum -c`; the probe transcript
contains no upload, filesystem write, partition 2, RockOS, Firefox, fallback,
network transfer, or `saveenv` action.

### Task 7: Final verification and publication

**Files:**
- Modify only files required by review findings.

- [ ] Review the complete diff normally against the Asterinas maintainability, development, security, hardware, and documentation persona indexes. Do not invoke the retired `aster-code-review` skill.
- [ ] Run the complete focused suite again in the persistent container and record exact commands/results. Confirm unrelated dirty worktree files were neither reverted nor included accidentally.
- [ ] Commit remaining fixes by exact path. Fetch `origin`, verify the branch is a fast-forward descendant of `origin/main`, and inspect every outgoing commit.
- [ ] Push `HEAD:main` without force only after all software gates and the one physical epoch pass. Re-fetch and require `origin/main == HEAD` before reporting completion.
