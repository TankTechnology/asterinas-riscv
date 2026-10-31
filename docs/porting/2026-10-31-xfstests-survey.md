# xfstests survey and RISC-V/ext4 port plan

## What xfstests is

`xfstests-dev` is the Linux ecosystem's filesystem regression suite.  It is
not a kernel test framework like ktest; it is a collection of shell/C test
programs driven by `./check`.  Each run needs:

- a **test device** and a **scratch device** (`TEST_DEV`, `SCRATCH_DEV`);
- mount points and a per-filesystem config loaded through `HOST_OPTIONS`;
- a run list (`short.list`, `full.list`, or a local list) and a blocklist;
- filesystem tools such as `mkfs`, `e2fsck`, `xfs_io`, `fsstress`, `fsx`, and
  the xfstests runtime dependencies.

`tests/generic` contains filesystem-independent tests; `tests/ext4`,
`tests/ext2`, and the other directories contain filesystem-specific tests.
A test number alone is not enough: the filesystem type, mkfs options, mount
options, and test/scratch images are part of the contract.

## What the current rebase line already has

The new upstream tree contains the modern integration:

- `test/initramfs/src/conformance/xfstests/run_xfstests.sh`: parses run lists,
  applies the per-filesystem config, mounts test/scratch, and calls `./check`.
- `test/initramfs/src/conformance/xfstests/<fs>/`: `config/build_config.mk`,
  `config/xfstests.config`, `prepare.sh`, and `run_list/*.list` for `ext2`,
  `tmpfs`, `overlay`, and `virtiofs`.
- `test/initramfs/nix/conformance/xfstests.nix`: pins xfstests 2026.06.21 and
  builds the suite plus its runtime dependencies.  The Nix `default.nix`
  supports `--argstr target riscv64` through `crossSystem`.
- `test/initramfs/Makefile`: builds the test/scratch images with
  `$(XFSTESTS_MKFS)` and the conformance initramfs.
- `tools/qemu_args.sh`: attaches x3/x4 test/scratch drives for the suites that
  request them.

The upstream CI workflow (`test_xfstests_full.yml`) runs **x86_64 ext2 only**.
`riscv64_pkgs` does not prebuild xfstests, and the RISC-V QEMU branch attaches
only x0/x1/x2 (ext2/exfat/ltp), not the xfstests x3/x4 devices.  RISC-V
xfstests is therefore not wired upstream yet.

## What the old branch has

The old branch carries the downstream ext4 gate from `8531fae58`
(`test(fs): add ext4 xfstests gate`):

- `test/initramfs/src/conformance/xfstests/ext4-pr.list` (40 tests:
  `ext4/042` plus generic 001..275);
- `local.config` with `FSTYP=${XFSTESTS_FS_TYPE:-ext2}`;
- `run_xfstests.sh` with a `FS_TYPE` mount and
  `ASTERINAS_XFSTESTS_OK fs=ext4 runlist=ext4-pr` marker;
- `AUTO_TEST=xfstests_ext4` in the top-level Makefile;
- `mkfs -t $(XFSTESTS_FS_TYPE)` for the test/scratch images;
- a blocklist (`generic/084`, `generic/226`, `generic/464`, `generic/524`,
  `generic/558`, `generic/590`, `generic/604`).

This was built against the **older flat** xfstests layout, which the new tree
replaced with per-filesystem directories.  No successful RISC-V ext4 xfstests
run is preserved in the old tree's evidence, so the old list is a starting
point, not a proven pass set.

## Port plan

### 1. Add the ext4 filesystem configuration (high priority)

Create `test/initramfs/src/conformance/xfstests/ext4/`:

- `config/build_config.mk`:
  `XFSTESTS_NEEDS_BLOCK_DEVICES := true` and an `XFSTESTS_MKFS` command that
  matches the current compatibility driver, e.g.
  `mke2fs -q -F -t ext4 -O ^metadata_csum,^64bit,^flex_bg,^orphan_file,^huge_file,^dir_nlink,^extra_isize`.
  Do not use a plain `mkfs.ext4` default: it enables features the driver
  deliberately rejects, and the failure would be a mkfs mismatch rather than
  a kernel bug.
- `config/xfstests.config`: `FSTYP=ext4`, TEST_DEV/SCRATCH_DEV,
  TEST_DIR/SCRATCH_MNT, `MOUNT_OPTIONS="-o noacl"`, `MKFS_OPTIONS="-F"`.
- `prepare.sh`: the same explicit mount/error-check as `ext2/prepare.sh`.
- `run_list/short.list`: start with the old `ext4-pr.list` (or a smaller
  subset) and `block.list`; add to `full.list` only after the short list is
  stable.

### 2. Wire RISC-V QEMU test/scratch devices

Extend the RISC-V branch of `tools/qemu_args.sh` to attach x3/x4 when
`XFSTESTS_NEEDS_BLOCK_DEVICES=true`.  RISC-V virtio-mmio devices are
discovered in reverse command-line order, so list them in reverse of the
desired `/dev/vda..vde` order (x4, x3, x2, x1, x0).  Keep the existing
ext2/exfat/ltp images so the conformance init's early mounts stay valid.

### 3. Add the riscv64 conformance artifact build

The xfstests package for riscv64 is buildable in principle: a Nix dry-run of
`conformance.xfstests` evaluates successfully and lists 182 cross-built
derivations.  The current probe builds the full xfstests conformance
initramfs:

```bash
nix-build --argstr target riscv64 --arg enableConformanceTest true \
  --argstr conformanceTestSuite xfstests \
  test/initramfs/nix/default.nix -A initramfs-image
```

The build log/out-link are `target/xfstests-riscv-build.log` and
`target/xfstests-riscv-initramfs`.  Add xfstests to `riscv64_pkgs` only if the
build is useful for the shared cache; do not push a public cache.

### 4. Add a RISC-V conformance boot path

Upstream `make run_kernel AUTO_TEST=conformance` defaults to x86
`grub-rescue-iso` and KVM.  For this line, use the RISC-V OSDK image and
`vmm-direct` (or a compact gate modeled on `desktop_gate.py`) to boot the
conformance initramfs with `/opt/run_conformance_test.sh` and the x3/x4
devices.  Verify the kernel passes the init arguments expected by
`test/initramfs/src/init`; if OSDK's `--init-args` path is not usable on
RISC-V, prefer a tiny test-only initramfs wrapper over changing the kernel
boot contract.

### 5. Triage, not blind expansion

Start with `ext4/042` and a small generic set (`generic/001`, `002`, `005`,
`007`, `011`, `013`, `014`), then expand to the old 40-test `ext4-pr` list.
Classify failures into:

- unsupported image features (fix mkfs/run list, not the kernel);
- unimplemented VFS/fs operations (kernel bug or documented gap);
- harness incompatibilities (df/mount/`findmnt` output, tools);
- hangs (move to `block.list` only after a bounded diagnosis).

Do not add tests to `block.list` merely to make the gate green; record the
reason and the source test.

## What not to port

- The old flat `local.config`/`ext4-pr.list`/`run_xfstests.sh` layout; the new
  per-filesystem layout and Nix package are newer and should be kept.
- The x86 CI/KVM workflow; this line validates QEMU locally.
- The old `ext4` kernel feature commits (`flex_bg`, `metadata_csum`, punch
  hole) until triage shows that a specific xfstests failure requires them.
- The whole upstream test catalog; start with a deterministic ext4 subset.

## Current status (2026-10-31)

The short ext4 list is green on QEMU/RISC-V:

```
ext4/042  3s   generic/001 24s   generic/002 3s   generic/005 3s
generic/007 7s generic/011 17s   generic/013 5s   generic/014 5s
Passed all 8 tests
All conformance tests passed.
```

Evidence: `~/.cache/asterinas-sync/evidence/2026-10-31-xfstests-ext4-short/`
(run log, QEMU log, initramfs hash, ktest log).  The run used the same OSDK
arguments as `make run_kernel AUTO_TEST=conformance CONFORMANCE_TEST_SUITE=xfstests
XFSTESTS_FS_TYPE=ext4 XFSTESTS_RUNLIST=short.list TARGET_ARCH=riscv64
INITRAMFS=on MEM=4G SMP=4 RELEASE=1 ENABLE_KVM=0`, with a prebuilt initramfs.

Commits on `feature/ext4-jbd2`:

- `66cd86630` test(xfstests): add ext4 coverage on RISC-V (profile, run lists,
  x3/x4 QEMU devices, contract tests).
- `73241d475` fix(ext2): match Linux ext4 identity and bsddf overhead
  (`FileSystem::name()` returns ext4; journal and reserved-GDT blocks are
  excluded from `f_blocks`; ktests added).
- `13fe9f1af` test(xfstests): make the cross-built RISC-V image runnable
  (target-bash repair for the three host-shebang scripts, `/dev/fd` alias,
  host test/baseline).

Ktest after the kernel change: **538 passed, 4 failed**.  The four failures
are the recorded baseline (two `ostd` simple-framebuffer, two `aster-nvme`);
`aster-core` is 182/182 and both new tests pass.

### Findings from the bring-up

- Cross-Nix `patchShebangs` left `check`, `src/dmerror`, and `ltp/rwtest.sh`
  with the **build-host x86_64 bash** shebang.  Executing `check` failed;
  bash's `ENOEXEC` fallback then re-executed `/proc/self/exe`, which in
  Asterinas points at the **script** rather than the interpreter, so the
  wrapper re-ran itself in a loop.  The packaging fix removes the trigger.
  The `/proc/self/exe` mismatch is a separate Linux-compatibility bug: it is
  set from `ProcessVm::new(executable_path)` before shebang resolution.
- The ext2/ext4 driver reported every mount as `ext2`, so xfstests rejected
  the ext4 test device; the ext4 entry point now reports `ext4`.
- `bsddf` `statfs` omitted the internal journal and reserved GDT blocks;
  ext4/042's block-count window failed until both were counted.
- `/dev/fd` was missing from the initramfs, so the `-E` blocklist could not
  be loaded through process substitution.  The init now creates the alias.

### Next

1. Run `full.list` (the 40-case downstream list) and triage each failure.
   `block.list` currently inherits the ext2 exclusions and is untriaged for
   ext4.
2. Add a Makefile/entry-point target if the `make run_kernel` path needs
   adjustments beyond the direct OSDK invocation.
3. Decide whether to add `conformance.xfstests` to `riscv64_pkgs` or keep the
   build local-only.
