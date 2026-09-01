# RISC-V Debian Rootfs Builder Image Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide a reproducible Docker-derived image and runtime entrypoint that can build and verify Debian riscv64 root filesystems without repeating ad-hoc package and binfmt setup.

**Architecture:** Keep the general Asterinas development image unchanged and add a dedicated `tools/docker/riscv-rootfs` image derived from the pinned RISC-V cross/DTC image. The image contains build-time packages and Debian keyring; an entrypoint performs the runtime-only `binfmt_misc` mount/registration and fail-fast validation, while persistent Docker volumes hold apt/debootstrap caches.

**Tech Stack:** Dockerfile, Bash entrypoint, GNU Make, Markdown documentation, Python unittest.

---

### Task 1: Add the dedicated builder image definition

**Files:**
- Create: `tools/docker/riscv-rootfs/Dockerfile`
- Create: `tools/docker/riscv-rootfs/entrypoint.sh`
- Create: `tools/docker/riscv-rootfs/README.md`

- [ ] **Step 1: Define the image inputs and package contract**

  Use a `BASE_IMAGE` build argument defaulting to the locally validated
  `asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached` image. Install
  `debootstrap`, `qemu-user-static`, `binfmt-support`, `debian-archive-keyring`,
  `gpgv`, `ca-certificates`, `curl`, `cpio`, `e2fsprogs`, `debugfs`, `file`,
  `jq`, `device-tree-compiler`, `qemu-system-misc`,
  `gcc-riscv64-linux-gnu`, `libc6-dev-riscv64-cross`, and
  `linux-libc-dev-riscv64-cross` with `--no-install-recommends`, then remove
  apt lists. Create root-owned cache mount points at
  `/var/cache/asterinas/debian` and `/var/cache/asterinas/debootstrap`.

- [ ] **Step 2: Implement runtime binfmt validation**

  The entrypoint must mount `binfmt_misc` if it is not already mounted, run
  `update-binfmts --enable qemu-riscv64` when the registration is absent or
  disabled, and reject the environment unless the registration is enabled,
  contains a RISC-V QEMU interpreter, and includes the `F` flag. It must also
  verify the required commands and root-owned keyring before executing the
  caller command. `--check` runs validation and exits without executing a
  command; all other arguments are passed through with `exec`.

- [ ] **Step 3: Document image build and run contract**

  Document the image tag, `docker build` command, required
  `--privileged --network=host` runtime flags, binfmt meaning, cache volume
  mounts, proxy pass-through, and examples for invoking
  `tools/riscv/debian/rootfs/build_rootfs.sh`. State explicitly that
  `qemu-riscv64-static` is a host-side build dependency and is removed from
  the generated guest rootfs.

- [ ] **Step 4: Run shell and Dockerfile static checks**

  Run `bash -n tools/docker/riscv-rootfs/entrypoint.sh` and
  `docker build --check` (or the available Dockerfile parser check). Expected:
  no syntax or parser errors.

### Task 2: Add a reproducible Make entry point

**Files:**
- Modify: `Makefile` near the existing Docker/development targets
- Modify: `tools/docker/README.md`

- [ ] **Step 1: Add image variables and target**

  Add `RISCV_ROOTFS_BASE_IMAGE` and `RISCV_ROOTFS_IMAGE` variables and a
  `build_riscv_rootfs_image` target that invokes `docker build` with the
  dedicated Dockerfile, passes `BASE_IMAGE`, and tags the derived image.
  Keep the target explicit so ordinary `make` and the general development
  image remain unchanged.

- [ ] **Step 2: Document the target and cache lifecycle**

  Add the target to the Docker README, including the first-run package
  download cost, reuse of the derived image, and safe cache volume names.

- [ ] **Step 3: Verify the Make target command generation**

  Run `make -n build_riscv_rootfs_image` and confirm the command references
  `tools/docker/riscv-rootfs/Dockerfile`, the configured base image, and the
  derived image tag without modifying the host.

### Task 3: Add contract tests and perform an image smoke test

**Files:**
- Create: `tools/riscv/tests/test_riscv_rootfs_builder_image.py`
- Modify: `Makefile` test target section

- [ ] **Step 1: Test the image contract textually**

  Add unittest coverage that checks the Dockerfile contains the required
  package names, the entrypoint checks `qemu-riscv64`, `flags:.*F`, and the
  keyring path, and the README documents `--privileged` and cache mounts.
  These tests must not require Docker or network access.

- [ ] **Step 2: Add a local unit target**

  Add `test_riscv_rootfs_builder_image_unit` to run the new unittest module
  with `python3 -W error::ResourceWarning -m unittest -v`.

- [ ] **Step 3: Build and run the derived image**

  Run `make build_riscv_rootfs_image`, then
  `docker run --rm --privileged --network=host` with the repository mounted
  and invoke the entrypoint's `--check`. Expected output must show enabled
  `qemu-riscv64` binfmt with `F`, all required commands, and the safe Debian
  keyring.

- [ ] **Step 4: Run repository checks**

  Run the new unit target, `git diff --check`, and the relevant rootfs unit
  tests. Record the image digest and check output in the implementation
  commit message or accompanying verification note; do not claim a complete
  Debian/Firefox boot until the existing rootfs gate is run separately.

### Task 4: Commit the isolated change

**Files:** all files from Tasks 1–3.

- [ ] **Step 1: Review the diff for scope and safety**

  Confirm that the general Dockerfile is unchanged, no host proxy or board
  state is persisted in the image, and generated `target/` artifacts are not
  staged.

- [ ] **Step 2: Commit**

  ```bash
  git add tools/docker/riscv-rootfs tools/docker/README.md \
    tools/riscv/tests/test_riscv_rootfs_builder_image.py Makefile \
    docs/superpowers/plans/2026-09-01-riscv-rootfs-builder-image.md
  git commit -m "build(riscv): add reproducible Debian rootfs image"
  ```
