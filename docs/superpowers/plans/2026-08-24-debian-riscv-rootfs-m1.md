# Debian RISC-V Rootfs M1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Boot a signed, auditable Debian trixie/riscv64 minbase filesystem on current Asterinas RISC-V, enter a dynamic Debian Bash, and prove writable-root persistence across two bounded QEMU boots at SMP=4.

**Architecture:** A small static stage1 initramfs discovers an ext2 disk by filesystem label, mounts the real Debian root, mounts the required pseudo-filesystems, and enters Debian with `chroot`. A host-side gate creates an immutable run copy of a validated base image, drives two no-network QEMU boots, checks Debian package identity and shell execution, writes a nonce on boot one, and verifies the same nonce on boot two. Build-time network access and runtime gate execution are deliberately separate.

**Tech Stack:** Python 3 `unittest`, POSIX shell, static C, Debian `debootstrap`, `gpgv`, `qemu-riscv64-static`, ext2/e2fsprogs, Asterinas OSDK, QEMU `virt`, U-Boot, HMP, RISC-V Sv39/SMP=4.

---

## Scope and file map

Create:

- `tools/riscv/debian/rootfs/__init__.py`
- `tools/riscv/debian/rootfs/contract.py`
- `tools/riscv/debian/rootfs/build_rootfs.sh`
- `tools/riscv/debian/rootfs/stage1_init.c`
- `tools/riscv/debian/rootfs/build_stage1.sh`
- `tools/riscv/debian/rootfs/gate_protocol.py`
- `tools/riscv/debian/rootfs/gate_runtime.py`
- `tools/riscv/debian/rootfs/rootfs_gate.py`
- `tools/riscv/debian/rootfs/README.md`
- `tools/riscv/tests/test_debian_rootfs.py`

Modify only:

- `Makefile`
- `tools/riscv/README.md`

Do not modify kernel code speculatively. If the real gate exposes a kernel defect, stop at the failing evidence, add the smallest focused regression, and revise this plan before changing kernel behavior.

## Task 1: Define the Debian rootfs identity contract

**Files:**

- Create: `tools/riscv/debian/rootfs/__init__.py`
- Create: `tools/riscv/debian/rootfs/contract.py`
- Create: `tools/riscv/tests/test_debian_rootfs.py`
- Modify: `Makefile`

- [ ] **Step 1: Add the unit-test target and failing contract tests**

Add this target to `Makefile`:

```make
.PHONY: test_riscv_debian_rootfs_unit
test_riscv_debian_rootfs_unit:
	@python3 -W error::ResourceWarning -m unittest \
		tools.riscv.tests.test_debian_rootfs -v
```

In `tools/riscv/tests/test_debian_rootfs.py`, add strict tests for:

- accepted manifest schema and package lock;
- missing and unknown JSON keys;
- boolean values where integers are required;
- non-HTTPS mirror or provenance URLs;
- wrong suite, architecture, filesystem type, label, size, or block size;
- malformed SHA-256 values;
- duplicate or unsorted package entries;
- manifest/package-lock version mismatch;
- base-image size/hash mismatch.

- [ ] **Step 2: Run the test to verify RED**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_rootfs -v
```

Expected: nonzero exit because `tools.riscv.debian.rootfs.contract` does not exist.

- [ ] **Step 3: Implement the smallest immutable identity model**

In `contract.py`, define frozen `FilesystemIdentity` and `RootfsManifest` dataclasses. Define `ROOT_LABEL = "ASTER_DEBIAN_ROOT"`; the explicit install tuple `bash`, `ca-certificates`, `coreutils`, `procps`, and `util-linux`; the gate identity tuple `base-files`, `libc6`, `bash`, `coreutils`, and `util-linux`; and an anchored lowercase SHA-256 regular expression.

Implement these typed operations:

- `sha256_file(path: Path) -> str` using bounded chunks;
- `load_manifest(path: Path) -> RootfsManifest` with exact-key validation;
- `parse_packages_lock(path: Path)`, returning an immutable sequence of `(name, architecture, version)` rows;
- `validate_frozen_root(image, manifest, packages_lock) -> RootfsManifest`.

The validator must require Debian `trixie`, `riscv64`, ext2, 4096-byte blocks, the exact label, a positive 1 GiB image size, HTTPS provenance, exact locked versions for every gate identity package, sorted unique `(name, architecture, version)` rows for every installed dpkg package, and matching image/package hashes. Reject Python `bool` explicitly where an integer is required. Use `hmac.compare_digest` for digest comparisons.

- [ ] **Step 4: Run focused GREEN and static checks**

Run:

```bash
make test_riscv_debian_rootfs_unit
python3 -m py_compile \
  tools/riscv/debian/rootfs/contract.py \
  tools/riscv/tests/test_debian_rootfs.py
ruff check tools/riscv/debian/rootfs tools/riscv/tests/test_debian_rootfs.py
ruff format --check tools/riscv/debian/rootfs tools/riscv/tests/test_debian_rootfs.py
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit the contract**

```bash
git add Makefile tools/riscv/debian/rootfs/__init__.py \
  tools/riscv/debian/rootfs/contract.py \
  tools/riscv/tests/test_debian_rootfs.py
git commit -m "test(riscv): define Debian rootfs identity contract"
```

## Task 2: Build a signed Debian minbase ext2 image

**Files:**

- Create: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Modify: `tools/riscv/debian/rootfs/contract.py`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] **Step 1: Add failing builder CLI and artifact tests**

Test these exact CLI forms:

```text
build_rootfs.sh [--output-dir DIR] [--cache-dir DIR] [--mirror HTTPS_URL] [--suite trixie]
build_rootfs.sh --print-tools
build_rootfs.sh --print-packages
```

Assert that `--print-tools` reports exactly `debootstrap`, `qemu-riscv64-static`, `gpgv`, `dpkg-query`, `mke2fs`, `dumpe2fs`, `debugfs`, `sha256sum`, and `curl`. Assert that `--print-packages` reports the explicit install tuple from Task 1. Add rejection tests for unknown arguments, HTTP mirrors, unsupported suites, missing tools, unsafe output paths, invalid `SOURCE_DATE_EPOCH`, and preservation of an existing published image on build failure.

Add a manifest-writer test that verifies exact JSON keys and deterministic serialization.

- [ ] **Step 2: Run focused tests to verify RED**

```bash
make test_riscv_debian_rootfs_unit
```

Expected: nonzero exit because the builder and manifest writer are absent.

- [ ] **Step 3: Implement the rootfs builder**

Use these defaults:

- output directory: `target/debian-riscv/rootfs`;
- content-addressed package cache: `target/debian-riscv/cache`;
- mirror: `https://mirrors.tuna.tsinghua.edu.cn/debian`;
- suite/architecture/variant: `trixie`, `riscv64`, `minbase`;
- filesystem label: `ASTER_DEBIAN_ROOT`;
- filesystem UUID: `7b7ad749-77d0-4e59-89e4-e117244a70aa`;
- filesystem size: 1 GiB;
- filesystem block size: 4096.

The script must use `set -euo pipefail`, private `mktemp -d` staging, signal cleanup, exact argument parsing, and atomic publication. Before `debootstrap`, download the selected mirror's `dists/trixie/InRelease` with HTTPS-only curl and verify it with `gpgv` against `/usr/share/keyrings/debian-archive-keyring.gpg`. Pass the same keyring explicitly to debootstrap. Record and verify each selected `.deb` against the signed package indexes before admitting it to a content-addressed cache; validate the hash again before every cache reuse.

Run foreign debootstrap, copy `/usr/bin/qemu-riscv64-static` only for the second stage, execute the second stage through that emulator, install the exact package set, configure a Debian Bash rcfile that prints `__DEBIAN_ROOTFS_SHELL_READY__`, and remove the emulator from the staged root before imaging.

Generate a lock row for every installed package from the staged dpkg database with host `dpkg-query --admindir`, including name, architecture, and exact version and sorting under `LC_ALL=C`. Build the image with:

```bash
truncate -s 1G "$ROOT_TMP"
mke2fs -q -F -t ext2 -b 4096 -L ASTER_DEBIAN_ROOT \
  -U 7b7ad749-77d0-4e59-89e4-e117244a70aa \
  -d "$STAGE" "$ROOT_TMP"
```

Verify label, UUID, type, block size, image size, and required files using `dumpe2fs` and `debugfs`. Extend `contract.py` with a deterministic manifest-writer CLI. Its exact schema records suite, Debian release version, mirror, architecture, signed-metadata hash, package-lock hash, every downloaded package hash, filesystem type/label/UUID/size/block size, relevant tool versions, build timestamp, and final root-image hash. Then publish `debian-root.ext2`, `rootfs-manifest.json`, `packages.lock`, `source-metadata/InRelease`, and `source-metadata/package-checksums` by same-directory temporary files, `fsync`, and atomic rename.

- [ ] **Step 4: Run builder tests and a controlled real build**

First run host checks:

```bash
bash -n tools/riscv/debian/rootfs/build_rootfs.sh
make test_riscv_debian_rootfs_unit
git diff --check
```

Then, in the pinned cached development container with Clash proxy variables inherited, install only the named Debian tooling, enable the RISC-V binfmt handler, and run one builder invocation into the ignored canonical output directory. This is the only planned Debian download/build; preserve the verified result for Task 8 rather than rebuilding it. Validate:

```bash
python3 -m tools.riscv.debian.rootfs.contract verify \
  --image "$OUT/debian-root.ext2" \
  --manifest "$OUT/rootfs-manifest.json" \
  --packages-lock "$OUT/packages.lock"
dumpe2fs -h "$OUT/debian-root.ext2"
debugfs -R 'stat /bin/bash' "$OUT/debian-root.ext2"
debugfs -R 'stat /var/lib/dpkg/status' "$OUT/debian-root.ext2"
```

Expected: every command exits 0, the image is ext2/4096/1 GiB with the exact label and UUID, `/bin/bash` is dynamic RISC-V ELF, and the dpkg database exists.

- [ ] **Step 5: Commit the signed builder**

```bash
git add tools/riscv/debian/rootfs/build_rootfs.sh \
  tools/riscv/debian/rootfs/contract.py \
  tools/riscv/tests/test_debian_rootfs.py
git commit -m "build(riscv): assemble signed Debian rootfs"
```

## Task 3: Implement the stage1 root handoff

**Files:**

- Create: `tools/riscv/debian/rootfs/stage1_init.c`
- Create: `tools/riscv/debian/rootfs/build_stage1.sh`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] **Step 1: Add failing native stage1 state-machine tests**

Compile `stage1_init.c` with a self-test macro and add cases for: one valid device, no match, two matching devices, bad ext2 magic, wrong label, non-block device, delayed valid device, root mount failure, `/dev` bind failure, each pseudo-filesystem mount failure, `chroot` failure, `chdir` failure, `exec` failure, and the 30-second discovery deadline.

Add a normal-lifecycle test that confirms a stable failure marker is flushed once and the process remains alive until terminated. Add builder tests for deterministic raw `newc`, exact entries `.` and `init`, root mode 0755, init mode 0755, archive mode 0644, `SOURCE_DATE_EPOCH`, spaced output paths, directory destinations, and failure preservation.

- [ ] **Step 2: Run tests to verify RED**

```bash
make test_riscv_debian_rootfs_unit
```

Expected: nonzero exit because the C source and builder do not exist.

- [ ] **Step 3: Implement exact ext2 discovery and handoff**

In C, open `/dev/console` for standard input, output, and error. Use these ext2 constants: superblock offset 1024, magic offset 56, volume-label offset 120, label length 16, and magic `0xef53`. Scan `/dev/vda` through `/dev/vdz`, require block devices, read the complete superblock with `pread`, and accept only the exact label. Retry only while there is no match; fail immediately on ambiguity. Use a 30-second monotonic deadline.

After discovery, mount the root ext2 read-write, bind-mount `/dev`, mount `proc` at `/proc`, `sysfs` at `/sys`, and `tmpfs` at both `/run` and `/tmp`; then `chroot`, `chdir("/")`, and execute Debian Bash with the configured rcfile. Every terminal failure prints one stable `DEBIAN_ROOTFS_FAIL reason=<reason>` line, flushes stdout, and enters an EINTR-safe pause loop. The self-test build returns normally after assertions.

Implement `build_stage1.sh` with the same strict CLI, atomic-publication, deterministic-time, permission, and destination checks proven by its tests. It must cross-compile a static RISC-V `/init` and produce an uncompressed raw `newc` archive containing only `.` and `init`.

- [ ] **Step 4: Verify native, archive, and cross builds**

```bash
make test_riscv_debian_rootfs_unit
bash -n tools/riscv/debian/rootfs/build_stage1.sh
cc -static -Wall -Wextra -Werror \
  tools/riscv/debian/rootfs/stage1_init.c -o /tmp/debian-stage1-host
python3 -m py_compile tools/riscv/tests/test_debian_rootfs.py
git diff --check
```

In the cached RISC-V container, install only the cross libc/UAPI headers if absent, build the archive, and verify:

```bash
cpio --quiet -it < target/debian-riscv/stage1/initramfs.cpio
file target/debian-riscv/stage1/init
```

Expected archive listing: exactly `.` and `init`; expected init: statically linked RISC-V ELF.

- [ ] **Step 5: Commit stage1**

```bash
git add tools/riscv/debian/rootfs/stage1_init.c \
  tools/riscv/debian/rootfs/build_stage1.sh \
  tools/riscv/tests/test_debian_rootfs.py
git commit -m "feat(riscv): hand off to Debian ext2 root"
```

## Task 4: Define the two-boot QEMU protocol

**Files:**

- Create: `tools/riscv/debian/rootfs/gate_protocol.py`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] **Step 1: Add failing argv, shell-command, and transcript tests**

Assert that QEMU argv contains the registered generic-Sv39 CPU contract, 2 GiB RAM, `-smp 4`, serial stdio, `-no-reboot`, `-display none`, and `-nic none`. It must contain exactly two block devices: a read-only ext4 boot disk and a writable ext2 root disk with `cache=directsync`, both attached with VirtIO block. It must not contain xHCI, USB input, graphics input, or networking devices.

Add rejection tests for SMP values other than 4, comma-bearing paths, symlinks, missing regular files, and DTBs whose enabled CPU count is not exactly four. Add transcript cases for echoed text, stale markers, reversed markers, nonzero commands, wrong Debian version/package versions, panic markers, I/O errors, oversized output, and a missing or mismatched persistence nonce.

- [ ] **Step 2: Run focused tests to verify RED**

```bash
make test_riscv_debian_rootfs_unit
```

Expected: nonzero exit because `gate_protocol.py` is absent.

- [ ] **Step 3: Implement pure protocol construction and classification**

Define frozen `ShellCommand`, `BootEvidence`, and `GateResult` types. Implement `qemu_argv`, `shell_commands`, and `classify_boot` as pure functions. The first boot must run `uname -m`, read `/etc/debian_version`, print `BASH_VERSION`, query locked versions of `base-files`, `libc6`, `bash`, `coreutils`, and `util-linux`, run `stat -f /`, create `/var/lib/asterinas-debian-m1`, write a generated nonce to its `persist` file, and call `sync`. The second boot repeats identity checks, reads the identical nonce without rewriting it, creates a second probe file, and calls `sync`.

Every command uses unique begin/end markers and emits its exit status. Classification must consume markers monotonically, reject duplicates and stale or reordered output, and scan the entire drained transcript for kernel panic, reboot, ext2, block-I/O, and stage1 failure markers. Limit each command payload to 64 KiB and the complete serial transcript to 8 MiB.

- [ ] **Step 4: Run GREEN and static checks**

```bash
make test_riscv_debian_rootfs_unit
python3 -m py_compile \
  tools/riscv/debian/rootfs/gate_protocol.py \
  tools/riscv/tests/test_debian_rootfs.py
ruff check tools/riscv/debian/rootfs/gate_protocol.py \
  tools/riscv/tests/test_debian_rootfs.py
ruff format --check tools/riscv/debian/rootfs/gate_protocol.py \
  tools/riscv/tests/test_debian_rootfs.py
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit the protocol**

```bash
git add tools/riscv/debian/rootfs/gate_protocol.py \
  tools/riscv/tests/test_debian_rootfs.py
git commit -m "test(riscv): define Debian two-boot gate protocol"
```

## Task 5: Implement bounded gate runtime primitives

**Files:**

- Create: `tools/riscv/debian/rootfs/gate_runtime.py`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] **Step 1: Add failing real-process lifecycle tests**

Use short-lived local subprocesses, pseudo terminals, and Unix sockets to cover:

- bounded connect, prompt, command, boot, and drain deadlines;
- split serial and HMP markers;
- HMP response byte caps;
- writable-parent rename and symlink swaps;
- stale-result invalidation before input validation;
- atomic replacement plus directory `fsync`;
- SIGTERM/SIGHUP deferral through cleanup and result publication;
- unblocked SIGHUP/SIGTERM masks in launched children;
- cleanup when the process leader already exited but group members remain;
- TERM-to-KILL escalation for stubborn groups;
- teardown order: monitor close, process-group cleanup, then serial EOF drain.

Run lifecycle tests three times to catch timing regressions without lengthening production timeouts.

- [ ] **Step 2: Run focused tests to verify RED**

```bash
make test_riscv_debian_rootfs_unit
```

Expected: nonzero exit because the runtime types and operations are absent.

- [ ] **Step 3: Implement the bounded runtime**

Define typed `GateTermination`, `MonitorError`, and `EarlyProcessExit` exceptions, plus `PinnedOutputDirectory`, `SerialConsole`, `HmpMonitor`, and `TerminationSignalState`. Implement process launch and process-group cleanup operations.

Pin the output directory using `O_PATH | O_DIRECTORY | O_NOFOLLOW`; perform invalidation, temporary creation, hashing, copying, and atomic result replacement relative to the pinned directory descriptor. Pass only required descriptors to child processes. Keep total monotonic deadlines across partial reads, cap response buffers, and drain serial output through EOF or a bounded drain deadline.

Launch QEMU in a new session. On every exit path close HMP, terminate the complete process group with TERM then bounded KILL escalation, reap the leader, and drain serial. Defer the first scoped termination signal until teardown is safe, hard-exit on the second, restore handlers, and ensure child processes inherit normal unblocked SIGHUP/SIGTERM masks. Do not use `pthread_sigmask` around process creation.

- [ ] **Step 4: Run repeated GREEN and static checks**

```bash
for run in 1 2 3; do
  make test_riscv_debian_rootfs_unit || exit 1
done
python3 -m py_compile \
  tools/riscv/debian/rootfs/gate_runtime.py \
  tools/riscv/tests/test_debian_rootfs.py
ruff check tools/riscv/debian/rootfs/gate_runtime.py \
  tools/riscv/tests/test_debian_rootfs.py
ruff format --check tools/riscv/debian/rootfs/gate_runtime.py \
  tools/riscv/tests/test_debian_rootfs.py
git diff --check
```

Expected: all three test runs and all static checks exit 0 with no leaked child processes.

- [ ] **Step 5: Commit the runtime**

```bash
git add tools/riscv/debian/rootfs/gate_runtime.py \
  tools/riscv/tests/test_debian_rootfs.py
git commit -m "feat(riscv): bound Debian gate lifecycle"
```

## Task 6: Orchestrate the persistent-root gate

**Files:**

- Create: `tools/riscv/debian/rootfs/rootfs_gate.py`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] **Step 1: Add failing dependency-injected orchestration tests**

Test this exact lifecycle order:

1. invalidate stale result evidence;
2. pin and snapshot all immutable inputs;
3. validate the base root image and DTB;
4. prepare the boot disk and writable root copy;
5. launch boot one, drive U-Boot, enter Debian, execute checks, write and sync nonce;
6. request HMP quit, close monitor, clean the process group, drain serial;
7. launch boot two against the same writable root, repeat identity checks, verify nonce;
8. quit, clean, drain, hash the final root, and atomically publish logs and result.

Cover preparation failure, launch failure, U-Boot timeout, shell timeout, command failure, post-PASS panic during drain, cleanup failure, final-root hash failure, interrupted publication, and success. Assert that no failure path leaves a passing result.

- [ ] **Step 2: Run focused tests to verify RED**

```bash
make test_riscv_debian_rootfs_unit
```

Expected: nonzero exit because `rootfs_gate.py` and its orchestration API are absent.

- [ ] **Step 3: Implement configuration, preparation, and two-boot orchestration**

Define a frozen `GateConfig` containing kernel, U-Boot, DTB, stage1 initramfs, root image, root manifest, packages lock, package checksums, output directory, `smp=4`, and explicit boot/command/cleanup timeouts. The CLI must require all eight input paths and the output directory; it must have no mode that downloads or rebuilds the Debian root.

Snapshot all inputs using `O_NOFOLLOW` descriptors and validate hashes before launching helpers. Build a 64 MiB ext4 boot disk containing exactly `asterinas.booti`, `qemu-virt.dtb`, and `stage1-initramfs.cpio`; remove `lost+found` and inspect the real filesystem image. Create the writable root via:

```bash
cp --reflink=auto --sparse=always -- \
  "/proc/self/fd/$ROOT_IMAGE_FD" "$RUN_ROOT_TMP"
```

Hash the source before and after copying, validate the copy against the frozen manifest, set mode 0600, and atomically rename it within the pinned output directory. Drive the registered `generic-sv39-ltp-smp4` CPU and an exact four-hart DTB. Use kernel boot arguments that select `init=/init` and surface warnings. Verify the DTB with `fdtget` before QEMU starts.

Publish only `boot.ext4`, `debian-root.run.ext2`, `boot1.serial.log`, `boot2.serial.log`, and `result.json`. The JSON must include input and final-root hashes, manifest identity, package identity, QEMU argv, per-phase durations, nonce hash rather than nonce plaintext, pass/fail, and a stable reason. Failure results retain the complete transcript available at cleanup and attempted argv; no lifecycle or publication failure may report `passed: true`.

- [ ] **Step 4: Run GREEN and artifact-focused checks**

```bash
make test_riscv_debian_rootfs_unit
python3 -m py_compile \
  tools/riscv/debian/rootfs/rootfs_gate.py \
  tools/riscv/tests/test_debian_rootfs.py
ruff check tools/riscv/debian/rootfs/rootfs_gate.py \
  tools/riscv/tests/test_debian_rootfs.py
ruff format --check tools/riscv/debian/rootfs/rootfs_gate.py \
  tools/riscv/tests/test_debian_rootfs.py
git diff --check
```

Expected: all commands exit 0; real helper tests confirm the boot disk contains exactly three payloads and the writable root copy remains sparse where supported.

- [ ] **Step 5: Commit the orchestrator**

```bash
git add tools/riscv/debian/rootfs/rootfs_gate.py \
  tools/riscv/tests/test_debian_rootfs.py
git commit -m "test(riscv): automate Debian persistent root gate"
```

## Task 7: Document and expose the operator workflow

**Files:**

- Create: `tools/riscv/debian/rootfs/README.md`
- Modify: `tools/riscv/README.md`
- Modify: `Makefile`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] **Step 1: Add failing documentation and Make-target tests**

Assert that the documentation contains runnable commands for:

- Clash proxy discovery at `127.0.0.1:17892` without persisting proxy configuration;
- TUNA as default, USTC as fallback, and official Debian as fallback;
- installing the exact host, cross-build, e2fsprogs, debootstrap, signature, and QEMU dependencies;
- enabling and checking RISC-V binfmt;
- building the frozen Debian root once;
- building current-main Sv39/SMP=4 kernel, DTB, U-Boot, and stage1;
- running the unit gate and the explicit two-boot runtime gate;
- inspecting manifest, package lock, logs, result JSON, and final root hash;
- distinguishing build-time network use from the runtime `-nic none` contract.

Add a Makefile test proving that the runtime target refuses missing artifact variables and never invokes a network builder implicitly.

- [ ] **Step 2: Run focused tests to verify RED**

```bash
make test_riscv_debian_rootfs_unit
```

Expected: nonzero exit because the guide and runtime target are absent.

- [ ] **Step 3: Write the exact operator guide and Make targets**

Document this pinned image:

```text
asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached
```

The container setup command must inherit the detected Clash variables and install the named packages: `debootstrap`, `qemu-user-static`, `binfmt-support`, `debian-archive-keyring`, RISC-V cross libc/UAPI headers, `cpio`, `e2fsprogs`, `curl`, and `gpgv`. Show `update-binfmts --enable qemu-riscv64` followed by a read-only verification before any rootfs build.

Add a `make test_riscv_debian_rootfs_unit` index entry and an explicit runtime target whose required variables name every frozen artifact. The runtime target may validate and execute the gate, but it must never call `debootstrap`, a mirror, or the rootfs builder.

Document cleanup semantics: base `debian-root.ext2` is immutable; only `debian-root.run.ext2` is writable; successful evidence remains; failed runs retain logs and a failing result but never mutate the base image.

- [ ] **Step 4: Verify documentation, unit targets, and neighboring gates**

```bash
make test_riscv_debian_rootfs_unit
make test_riscv_xhci_input_unit
python3 -m py_compile tools/riscv/debian/rootfs/*.py
bash -n tools/riscv/debian/rootfs/build_rootfs.sh
bash -n tools/riscv/debian/rootfs/build_stage1.sh
ruff check tools/riscv/debian/rootfs tools/riscv/tests/test_debian_rootfs.py
ruff format --check tools/riscv/debian/rootfs tools/riscv/tests/test_debian_rootfs.py
git diff --check
```

Expected: all commands exit 0 and the xHCI input unit gate remains green.

- [ ] **Step 5: Commit documentation and targets**

```bash
git add Makefile tools/riscv/README.md \
  tools/riscv/debian/rootfs/README.md \
  tools/riscv/tests/test_debian_rootfs.py
git commit -m "docs(riscv): document Debian persistent root gate"
```

## Task 8: Build the real image and execute the M1 gate

**Files:**

- Modify: `tools/riscv/debian/rootfs/README.md`
- Modify only if a focused regression requires it: files created in Tasks 1 through 6

- [ ] **Step 1: Run a no-network preflight before expensive work**

Run the complete host unit suite, static checks, builder `--print-tools`, builder `--print-packages`, and gate `--help`. Check the cached image, Clash endpoint, free disk space, expected ignored output directories, and absence of leftover named containers or QEMU processes. Do not start the build until all preflight checks pass.

- [ ] **Step 2: Revalidate or, only if necessary, build one signed Debian root image**

Revalidate the preserved Task 2 root image and reuse it when its manifest, package lock, source metadata, and builder commit still match. Run `build_rootfs.sh` only if that artifact is missing, invalid, or the builder changed after Task 2; never rebuild it merely to repeat an already-passing check. When a build is required, default to TUNA through Clash. Use USTC or official Debian only if the preceding mirror fails with recorded evidence; do not change apt configuration globally.

After the build, run the contract validator, verify `source-metadata/package-checksums`, and run `dumpe2fs` plus targeted `debugfs` checks. Record the immutable base-image hash, manifest hash, package-lock hash, verified InRelease digest, mirror URL, suite, and build timestamp. Do not commit the 1 GiB image.

- [ ] **Step 3: Build current Sv39/SMP=4 boot artifacts once**

Using the pinned container and current branch, build stage1, current Asterinas kernel, a four-hart DTB matching registered `generic-sv39-ltp-smp4`, and U-Boot. Verify the DTB CPU count, kernel/initramfs hashes, U-Boot provenance, static stage1 ELF identity, and exact stage1 archive entries before QEMU.

- [ ] **Step 4: Run the bounded two-boot gate**

Invoke the gate with a 300-second outer timeout and all explicit frozen paths:

```bash
timeout --signal=TERM --kill-after=20s 300s \
  python3 tools/riscv/debian/rootfs/rootfs_gate.py \
    --kernel "$KERNEL" \
    --uboot "$UBOOT" \
    --dtb "$DTB" \
    --stage1 "$STAGE1" \
    --root-image "$ROOT_IMAGE" \
    --root-manifest "$ROOT_MANIFEST" \
    --packages-lock "$PACKAGES_LOCK" \
    --package-checksums "$SOURCE_METADATA/package-checksums" \
    --output-dir "$GATE_OUTPUT"
```

Observe named phase changes and live serial growth; do not use blind sleeps. Expected: exit 0, both boot logs contain ordered shell-ready and command-success evidence, boot two reads the boot-one nonce, `result.json` reports `passed: true`, and the final writable-root hash differs from the immutable base hash.

- [ ] **Step 5: Handle any real failure without expanding scope**

If the gate fails, apply `superpowers:systematic-debugging`: preserve logs, identify the first failing phase, reproduce with the smallest command, and add a RED regression before changing code. Do not edit the kernel under this task unless the failure proves a kernel defect and this plan is explicitly revised.

- [ ] **Step 6: Record reproducible evidence**

Add a dated evidence section to `tools/riscv/debian/rootfs/README.md` containing exact commands, hashes, package versions, gate durations, QEMU version, and the stable success markers. State clearly that no physical-board or network-runtime claim is made.

```bash
git add tools/riscv/debian/rootfs/README.md
git commit -m "docs(riscv): record Debian rootfs M1 evidence"
```

## Task 9: Review and finish the branch

**Files:**

- Create: `docs/superpowers/reviews/2026-08-24-debian-riscv-rootfs-m1.md`
- Modify only for confirmed review findings: files from Tasks 1 through 8

- [ ] **Step 1: Run final focused verification from a clean state**

```bash
make test_riscv_debian_rootfs_unit
python3 -m py_compile tools/riscv/debian/rootfs/*.py \
  tools/riscv/tests/test_debian_rootfs.py
bash -n tools/riscv/debian/rootfs/build_rootfs.sh
bash -n tools/riscv/debian/rootfs/build_stage1.sh
ruff check tools/riscv/debian/rootfs tools/riscv/tests/test_debian_rootfs.py
ruff format --check tools/riscv/debian/rootfs tools/riscv/tests/test_debian_rootfs.py
git diff --check
git status --short
```

Expected: all checks exit 0 and status is clean. Re-run `make test_riscv_xhci_input_unit` only if the branch changed its files after Task 7. Re-run the real QEMU gate only if implementation or review changes touched runtime behavior after Task 8; otherwise validate the preserved hashes and evidence rather than repeating an already proven expensive run.

- [ ] **Step 2: Review against Asterinas personas**

Use `aster-code-review` in diff mode against the branch base. Review maintainability, development correctness, security boundaries, hardware contracts, and documentation. Pay special attention to signature provenance, untrusted paths, descriptor pinning, ext2 parsing, process groups, signal races, bounded buffers/deadlines, evidence publication, and the separation of immutable base from writable run copy.

- [ ] **Step 3: Resolve confirmed findings with tests**

Use `superpowers:receiving-code-review` for every Important or Critical finding. Reproduce each confirmed issue with a focused failing test, implement the narrowest fix, and rerun the relevant focused and static checks. Record rejected findings with technical evidence rather than changing code defensively.

- [ ] **Step 4: Write and commit the review record**

Create `docs/superpowers/reviews/2026-08-24-debian-riscv-rootfs-m1.md` with commit range, findings, resolutions, exact verification results, real-gate evidence, and remaining limitations.

```bash
git add docs/superpowers/reviews/2026-08-24-debian-riscv-rootfs-m1.md
git commit -m "docs(riscv): review Debian rootfs M1"
```

- [ ] **Step 5: Present integration choices without mutating remote state**

Apply `superpowers:verification-before-completion`, then `superpowers:finishing-a-development-branch`. Report commits, tests, hashes, artifacts, and limitations. Offer merge/PR/keep/cleanup choices, but do not push, merge, delete the branch, or remove preserved evidence without explicit user selection.
