# FOUNDATION-M3 report — modern syscalls + mount semantics on RISC-V

> 2026-08-14. Corresponds to plan
> `docs/superpowers/plans/2026-08-13-nixos-riscv-track.md` (the "Nix package
> manager" milestone, M3). Conclusion first: the M3 smoke gate **passes 8/8**
> — `openat2`, `membarrier`, `mount_setattr`, `MS_SHARED`/`MS_SLAVE`
> propagation, `/proc/self/mountinfo`, and `pivot_root` (off the initramfs
> rootfs) all return correct results on a freshly built kernel.

## Deliverables

| File | Purpose |
|---|---|
| `kernel/src/syscall/openat2.rs` | `openat2(2)`: honours `RESOLVE_NO_SYMLINKS`, rejects unsupported `RESOLVE_*` |
| `kernel/src/syscall/membarrier.rs` | `membarrier(2)`: `QUERY` + private-expedited commands |
| `kernel/src/syscall/mount_setattr.rs` | `mount_setattr(2)`: attr flags + propagation |
| `kernel/src/fs/vfs/path/mount.rs` | `MountPropType::{Shared,Slave,Unbindable}` |
| `kernel/src/fs/vfs/path/resolver.rs` | `pivot_root` off the initramfs rootfs |
| `tools/riscv/fm3-gate.sh` | One-command gate: build → pack → QEMU boot → report |
| `tools/riscv/nixos/fm3/{init.c,build_fm3.sh,boot_fm3.py}` | In-guest smoke test + driver |

## How to run

```bash
tools/riscv/fm3-gate.sh                 # build initramfs, repack, boot, report
tools/riscv/fm3-gate.sh --smp 4         # SMP=4
tools/riscv/fm3-gate.sh --rebuild-kernel  # rebuild kernel first
```

The gate forces `OSDK_TARGET_ARCH=riscv64`, `--features riscv_sv39_mode`, the
prebuilt `vdso_riscv64.so`, and `rust-objcopy` on `PATH` (the three traps that
otherwise silently build the host x86_64 arch or fail the RISC-V Image step).

## Result (SMP=1)

```
[FM3] mountinfo: OK
[FM3] openat2: OK
[FM3] membarrier: OK
[FM3] mountprop_shared: OK
[FM3] mountprop_slave: OK
[FM3] mountprop_private: OK
[FM3] mount_setattr: OK
[FM3] pivot_root: OK
__FM3_DONE__ __FM3_PASS__
```

## What landed (commit-by-commit)

1. **`feat(syscall): openat2 and membarrier`** — `openat2(2)` reads `struct
   open_how`, validates the `resolve` field, maps `RESOLVE_NO_SYMLINKS` onto
   `O_NOFOLLOW` (approximates the all-components semantics on the final
   component only), and rejects the not-yet-supported bits (`NO_XDEV`,
   `NO_MAGICLINKS`, `BENEATH`, `IN_ROOT`, `CACHED`) with `EINVAL` so callers
   fall back to `openat`. `membarrier(2)` `QUERY` advertises the four
   private-expedited commands, which are no-ops on a framekernel without
   JIT-compiled code; global commands are rejected with `EINVAL`.

2. **`fix(vfs): pivot_root off the initramfs rootfs`** — `pivot_root(2)` must
   allow the *current* root to be the namespace root mount (the canonical
   initramfs → real-root case); only `new_root` may not be the rootfs mount.
   The old root's parent/mountpoint are captured before grafting (which
   mutates the parent link), and when the old root has no parent the new root
   is detached to become the new root mount.

3. **`feat(fs): mount_setattr(2) and MS_SHARED/MS_SLAVE propagation`** —
   `MountPropType` grows `Shared`/`Slave`/`Unbindable`; `mount(2)`'s
   change-type path now accepts `MS_SHARED`/`MS_SLAVE`/`MS_UNBINDABLE`
   (previously `EINVAL`). `mount_setattr(2)` maps `mount_attr.attr_set`/
   `attr_clr` to per-mount flags and `attr.propagation` to a propagation
   change; id-mapped mounts (`MOUNT_ATTR_IDMAP`, non-zero `userns_fd`) are
   rejected with `EOPNOTSUPP`.

4. **`test(riscv): FOUNDATION-M3 smoke gate harness`** — the in-guest `init`
   exercises each item and prints a `__FM3_<name>_{OK,FAIL}__` marker, so a
   crash/ENOSYS is attributed to the exact syscall.

5. **`fix(test): drop --release from fm3 gate`** — see "build traps" below.

## Known limitations (honest scope)

- **Peer-group event propagation is not implemented.** `MS_SHARED`/`MS_SLAVE`
  record the propagation type on the mount, but mount/unmount events are not
  mirrored across a peer group the way Linux does. The type is bookkeeping,
  not semantics. `/proc/self/mountinfo` does **not** yet emit the optional
  `shared:`/`master:`/`unbindable` fields (the 10 core fields are correct).
- **`openat2` `RESOLVE_NO_SYMLINKS` is final-component-only** (via
  `O_NOFOLLOW`), not the Linux all-components guarantee.
- **`membarrier` global commands** return `EINVAL` (no inter-CPU IPIs).

## Not yet done — next in priority order

1. **`seccomp` (syscall 317)** — currently `ENOSYS`. `SECCOMP_SET_MODE_STRICT`
   is small and real: add a `seccomp_mode` field to
   `PosixThread` (`kernel/src/process/posix_thread/mod.rs`), a `sys_seccomp`
   handler, and an allowlist check in `handle_syscall`
   (`kernel/src/syscall/mod.rs:387`) that delivers `SIGSYS` on violation.
   `SECCOMP_SET_MODE_FILTER` (BPF) is a much larger follow-up. Needed for the
   M4 nix-daemon sandbox, but the plan marks it bypassable.

2. **`keyctl`/`add_key`/`request_key` (219/217/218)** — `ENOSYS`. The kernel
   has no keyring subsystem; a minimal `keyctl(KEYCTL_GET_KEYRING_ID)` stub
   would unblock `libkeyutils`-based lookups, but nothing on the M3 `nix eval`
   path depends on it.

3. **`fanotify_init`/`fanotify_mark` (262/263)** — `ENOSYS`. Requires fsnotify
   infrastructure (only `inotify` exists today). Not on the M3 path.

4. **Peer-group propagation + `mountinfo` optional fields** — the real fix for
   the "bookkeeping only" limitation above.

## Build traps (documented so they are not re-hit)

- **`rust-objcopy` is not on `PATH`.** It lives under the rustup toolchain
  sysroot (`…/lib/rustlib/x86_64-unknown-linux-gnu/bin/`); the RISC-V Image
  step shells out to it. `fm3-gate.sh` now finds and adds it.
- **`OSDK_TARGET_ARCH` must be set.** Without `OSDK_TARGET_ARCH=riscv64`,
  `cargo osdk build` targets the host `x86_64` and produces a `qemu_elf`
  instead of a RISC-V `Image` — the build *succeeds* and silently gives you
  the wrong artifact.
- **`--release` breaks the riscv64 build.** `ostd`'s inline-asm Image-header
  check fails with `expected absolute expression` / `RISC-V Image header must
  be exactly 64 bytes`. Use the dev-profile build (the default), as all
  previous sessions did.
- **`VDSO_LIBRARY_DIR`** must point at `~/.local/share/linux_vdso`
  (`vdso_riscv64.so`).

## Reproduce

```bash
tools/riscv/fm3-gate.sh --smp 1
# serial transcript: target/nixos/fm3/fm3-serial.log.smp1
```
