# RISC-V LTP Layered Integration Design

Date: 2026-08-20
Status: Approved design
Target branch: a new branch based on `codex/megrez-usb-keyboard`

## Objective

Bring the existing RISC-V Linux Test Project work from
`origin/track/nixos` into the current Asterinas development line without
merging that branch wholesale. Establish a reproducible 767-test QEMU
baseline first, then integrate verified kernel fixes in reviewable batches,
and handle the loop-device subsystem as a separate high-risk change.

The work must preserve the current RISC-V desktop and Wayland artifacts and
must not mix DRM changes into the LTP integration branch.

## Current State

- The current branch and `origin/track/nixos` diverge from an old common base.
  The current branch has 129 unique commits and the remote track has 108.
  Merging the whole track would import unrelated NixOS, audio, systemd, and
  experimental work.
- The current LTP manifest has about 544 active entries. The remote M27 gate
  enables the expanded manifest and reports 767 runnable tests with a result
  of 549 PASS, 137 aggregate FAIL, and 81 CONF. The aggregate FAIL count
  includes 5 runner-classified CRASH and 3 TIMEOUT results, so the mutually
  exclusive counts are 549 PASS, 129 plain FAIL, 81 CONF, 5 CRASH, and 3
  TIMEOUT. This distinction is required to avoid double-counting results.
- The remote track contains a one-command RISC-V LTP gate, subset and minimal
  reproduction tools, and later fixes for network semantics, option pointer
  validation, helper packaging, clock and fallocate error returns, and loop
  devices.
- The current branch already contains a patch-equivalent implementation of
  the basic `clock_getres` syscall, but not the later NULL-pointer correction.
- No reusable `target/ltp` build exists in the current worktree. Required
  cross-build and QEMU tools are available through the local
  `asterinas-env:nixos-build` and `asterinas-env:uboot-sim` images.
- The historical LTP gate rewrites
  `target/qemu-uboot/current/boot.ext4`. That path is shared with the desktop
  demonstrations and cannot be used by the integrated gate.

## Chosen Approach

Use a layered transplant rather than a branch merge or a fresh rewrite.
Existing remote commits provide provenance and verified behavior, while the
integrated tooling is adapted to the current repository's bounded QEMU
session and artifact-validation infrastructure.

Each layer has an independent acceptance gate. A failed layer is fixed or
reverted before the next layer starts, so a regression can be attributed to
one small group of changes.

## Architecture

### 1. Isolated Integration Workspace

Create `codex/riscv-ltp-integration` from the current branch in an isolated
Git worktree. The existing branch, its ignored desktop artifacts, and all
untracked user files remain untouched until the completed integration is
reviewed and explicitly merged.

The implementation branch contains only LTP/RISC-V work. DRM follow-up uses a
different branch and design cycle.

### 2. LTP Build and Manifest Layer

Port the following remote capabilities in their original dependency order:

- riscv64 musl cross-compilation of the pinned LTP 20260529 source;
- dynamic `libltp.so` packaging so the initramfs remains small enough to boot;
- the in-guest watchdog runner and minimal `/init`;
- subset and raw-syscall reproduction commands;
- BusyBox helper integration where the image exists;
- execve and execveat runtime helper binaries;
- the expanded M27 manifest that produces 767 runnable syscall tests.

The source remains pinned to LTP 20260529, matching
`test/initramfs/nix/conformance/ltp.nix`. Updating LTP itself is outside this
integration because it would change both the test universe and expected
results.

### 3. QEMU Execution and Evidence Layer

The gate owns its artifacts beneath `target/ltp/qemu/`:

```text
target/ltp/
├── src/
├── build/
├── rootfs/
├── ltp-initramfs.cpio.gz
├── results/
│   └── <run-id>/
│       ├── serial.log
│       ├── summary.txt
│       ├── result.json
│       └── SHA256SUMS
└── qemu/
    ├── boot.ext4
    ├── manifest.json
    └── marker-event.txt
```

The gate stages a private boot disk and verifies the exact kernel, DTB, and
initramfs identities before boot. It reuses the current bounded serial-session
and process-group cleanup code. It never edits
`target/qemu-uboot/current/boot.ext4`.

Every run records:

- the Git commit and selected test set;
- SMP count, timeouts, QEMU version, and artifact hashes;
- each test verdict and the final counts;
- whether the guest reached the completion marker;
- whether QEMU cleanup completed.

The machine-readable result uses mutually exclusive `pass`, `fail`, `conf`,
`crash`, and `timeout` counters and validates
`total = pass + fail + conf + crash + timeout`. It additionally exposes a
`legacy_fail_total = fail + crash + timeout` field for comparison with the
remote runner's historical summary format.

A completed run with LTP failures is a valid baseline result and differs from
an infrastructure failure. Missing completion markers, kernel panics, QEMU
leaks, malformed summaries, or artifact mismatches make the gate itself fail.
The command also returns nonzero when test failures are present unless an
explicit baseline-recording option is used.

### 4. Verified Point-Fix Layer

After capturing the unmodified current-branch baseline, transplant verified
fixes in small, dependency-complete batches:

1. LTP packaging helpers for execve and execveat tests.
2. Socket error-code and NULL-option-buffer validation.
3. INADDR_ANY bind support and connect-to-unspecified-address resolution.
4. `clock_getres` NULL-timespec handling and fallocate offset-overflow errno.
5. Capability, `F_DUPFD`, and `PR_SET_NAME` boundary corrections.

Each batch must name the exact focused LTP tests and record before/after
verdicts. A batch is retained only when the expected failures move to PASS or
to their next independently classified failure and no previously passing test
regresses.

Large features from the remote track, including clone/cgroup, file handles,
rseq, scheduling policy changes, and lazy fork/COW work, are not silently
folded into these point-fix batches. They become later work only when the new
baseline proves they are still needed.

### 5. Loop-Device Layer

The loop-device subsystem is a separate implementation and review unit. Its
remote change is roughly 600 lines across device registration, ioctl routing,
file/inode handles, block partition parsing, and three follow-up commits.

Before integration, the layer receives maintainability, correctness,
security, and hardware-contract review. Acceptance covers:

- `/dev/loop-control` and `/dev/loopN` discovery;
- allocation, configuration, status query, clear, and reuse ioctls;
- zero-length backing files and zero-sector partition parsing;
- block-registry dispatch and teardown behavior;
- the complete LTP subset previously blocked by failure to acquire a loop
  device;
- a full 767-test regression run after the focused subset passes.

Loop-device work is never combined in the same commit with point fixes.

## Data Flow

1. The build step filters the repository manifest against LTP's
   `runtest/syscalls`, cross-builds the selected binaries, and packages their
   runtime dependencies into the initramfs.
2. The preparation step copies a selected RISC-V kernel, DTB, and initramfs
   into a private boot disk and writes a hash manifest.
3. The QEMU driver validates the manifest, boots through U-Boot, and captures a
   bounded serial stream.
4. The guest runner forks each test under a watchdog and emits structured
   verdict markers followed by one terminal summary marker.
5. The host parser validates the markers and publishes the serial transcript,
   human summary, machine-readable result, and hashes as one run directory.
6. Focused before/after results determine whether a fix batch is retained;
   full-suite results detect regressions outside the focused subset.

## Error Handling and Isolation

- Missing compilers, LTP sources, QEMU, U-Boot, kernel, DTB, or BusyBox are
  detected before a boot disk is published.
- Preparation uses a staging directory and publishes completed artifacts only
  after validation. Interrupted builds cannot replace the last valid run.
- All paths are resolved beneath the repository and the LTP-owned target
  directory. Cleanup never targets the workspace root or shared desktop
  artifacts.
- QEMU runs have bounded startup, firmware-command, guest, and termination
  timeouts. The entire process group is reaped on success, failure, timeout,
  and interruption.
- Serial output is bounded and a truncated transcript is classified as an
  infrastructure failure rather than a test failure.
- Test CONF, plain FAIL/TBROK, signal termination, per-test timeout, kernel
  panic, and whole-gate timeout remain distinct result classes. Compatibility
  reports may aggregate plain FAIL, CRASH, and per-test TIMEOUT only through
  the explicitly named `legacy_fail_total` field.
- A rerun uses a new run directory; prior evidence is immutable.

## Verification Strategy

### Host and Static Checks

- Unit-test manifest filtering, verdict parsing, count validation, artifact
  path isolation, timeout validation, and process cleanup.
- Run the complete `tools/riscv/tests` suite with the repository tools on
  `PYTHONPATH`.
- Run shell syntax checks and `git diff --check` for each tooling batch.
- Confirm the gate cannot write to `target/qemu-uboot/current` with a host
  regression test.

### Build Checks

- Build the RISC-V kernel in the prescribed project container.
- Cross-build LTP 20260529 and report build failures explicitly.
- Verify that every active manifest item is either packaged or reported as
  unavailable; silent omission is an error.
- Validate initramfs contents and artifact hashes before QEMU starts.

### Runtime Checks

1. Run a small smoke subset to validate boot, watchdogs, parsing, and cleanup.
2. Run the focused tests associated with each fix before and after the patch.
3. Run the complete 767-test suite at SMP=1 and publish the full result set.
4. Run an SMP=4 smoke/subset gate. If it completes without a kernel or runner
   hang, run the complete SMP=4 suite with a larger bounded timeout.
5. After loop-device integration, rerun its focused subset and both applicable
   full-suite gates.

The first integration milestone succeeds when the 767-test SMP=1 run completes
and produces internally consistent evidence, all expected point-fix movements
are demonstrated, no prior PASS regresses without classification, and the
existing RISC-V host test suite remains green. It does not require all 767 LTP
tests to pass; remaining failures become the evidence-backed work queue.

## Commit and Review Boundaries

Implementation commits follow these boundaries:

1. host tests that specify private artifacts and result parsing;
2. LTP build and guest runner tooling;
3. private QEMU preparation/execution and evidence publication;
4. expanded manifest and runtime resource packaging;
5. one commit per independent point-fix batch;
6. loop-device implementation and its follow-up corrections as a dedicated
   reviewed series;
7. result/report commits only after the corresponding logs exist.

Before local integration, the final range receives an Asterinas persona-based
code review and verification from a clean worktree. No push or remote branch
mutation is implied by this design.

## DRM Auxiliary Track

DRM work remains secondary and isolated. The first known candidate is the M16
defect where `DRM_IOCTL_SET_MASTER` succeeds on `/dev/dri/renderD128` instead
of failing with `EACCES`. It may be specified and implemented on its own branch
after the first LTP baseline is operational, or while an externally running
full LTP gate requires no code changes. It is not part of the LTP commits or
their acceptance criteria.

## Non-Goals

- Merging all of `origin/track/nixos` or `origin/integration`.
- Updating the pinned LTP version.
- Making every enabled LTP test pass in the first integration milestone.
- Reclassifying environment or musl differences as kernel fixes without a
  raw-syscall reproduction.
- Mixing DRM, desktop, NixOS, audio, or systemd changes into the LTP branch.
- Pushing branches, opening pull requests, or altering remote state without a
  separate user request.
