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

## Current status

- Code cleanup finished in `2b6546733`.
- Nix dry-run for riscv64 xfstests succeeded.
- Full riscv64 xfstests initramfs build started; results are appended to
  `target/xfstests-riscv-build.log`.
- No xfstests image/config/QEMU change has been committed yet; this note is
  the review point before the port.
