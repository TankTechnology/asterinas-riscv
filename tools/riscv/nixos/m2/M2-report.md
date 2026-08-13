# M2 Report: Dynamic musl Userspace on Asterinas RISC-V

> 2026-08-13. Corresponds to M2 of
> `docs/superpowers/plans/2026-08-13-nixos-riscv-track.md`.
> Conclusion up front: **a dynamically linked riscv64 musl binary runs on the
> Asterinas kernel** — ET_DYN exec, the musl loader (`ld-musl-riscv64.so.1`),
> and DT_NEEDED shared-library resolution all verified in QEMU. The static
> M1 busybox world coexists in the same initramfs.

## Deliverables (tools/riscv/nixos/m2/)

| File | Purpose |
|---|---|
| `libgreet.c` / `libgreet.h` | Shared library used to test DT_NEEDED resolution |
| `hello.c` | Dynamically linked main binary (musl libc + libgreet) |
| `init_m2.c` | `/init`: mounts /proc and /sys, runs hello_dyn, then busybox sh |
| `build_m2.sh` | Cross-compiles with `riscv64-linux-musl-gcc` and assembles the initramfs |
| `boot_m2_smoke.py` | QEMU boot driver: U-Boot booti handoff, serial marker checks |
| `M2-report.md` | This report |

Artifacts (`target/nixos/m2/`): `m2-initramfs.cpio.gz`, `hello_dyn`,
`libgreet.so`, `init`.

## Toolchain

- Arch package `musl-riscv64` (extra repo) provides
  `/usr/bin/riscv64-linux-musl-gcc` (gcc 15.1.0 with musl specs) and the
  riscv64 musl sysroot at `/usr/riscv64-linux-musl/`.
- **musl convention**: the dynamic loader *is* libc — a single shared object
  (`lib/musl/lib/libc.so`, 965 KiB) is copied into the initramfs as
  `/lib/ld-musl-riscv64.so.1`, which is the ELF interpreter recorded in the
  binaries (`readelf -l hello_dyn` shows `INTERP /lib/ld-musl-riscv64.so.1`).
- No proxy needed: the package comes from the TUNA/USTC pacman mirrors
  (see `../MIRRORS.md`).

## Reproduction

```bash
# 1. Build the dynamic world and the initramfs
tools/riscv/nixos/m2/build_m2.sh

# 2. Re-pack the boot disk with the M2 initramfs
export ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel-osdk-bin.Image"
export ASTERINAS_INITRAMFS="$PWD/target/nixos/m2/m2-initramfs.cpio.gz"
export QEMU_UBOOT_CACHE_DIR=/home/arch-anjie/Program/asterinas-riscv/target/qemu-uboot/cache
export QEMU_UBOOT_PROFILE=generic-sv39
tools/riscv/prepare_qemu_uboot_booti.sh prepare

# 3. QEMU smoke
python3 tools/riscv/nixos/m2/boot_m2_smoke.py
```

The kernel Image is reused from the sibling working tree (`asterinas-riscv`,
same HEAD); this tree only builds userspace.

## Results (3/3 checks pass)

| Check | Evidence | Status |
|---|---|---|
| execve of ET_DYN (PIE) binary | `__M2_HELLO_DYN__ hello from libgreet to riscv64` | ✅ |
| DT_NEEDED resolution (libgreet.so) | the greeting above comes from the shared library | ✅ |
| M1 static world coexists | `__M2_SHELL_OK__` printed by busybox sh | ✅ |

Serial log shows the expected syscall sequence for a dynamic start-up:
mprotect, prctl, rt_sigaction, clone, set_robust_list, wait4 — all serviced
by the kernel without ENOSYS warnings.

## What this unblocks

- M3 (Nix package manager): Nix is a dynamically linked program; its toolchain
  requirements (musl libc, dlopen-able libs, TLS) now have a verified path.
- Any musl-targeted riscv64 program can be dropped into this initramfs pattern
  for a quick smoke test.

## Known gaps (unchanged from M1)

- Interactive shell still requires termios (TCGETS/TCSETS) hardening.
- `/proc/self` magic symlink is still missing.
- `riscv_hwprobe`/`rseq` return ENOSYS (harmless startup probes for glibc;
  musl does not issue them).
