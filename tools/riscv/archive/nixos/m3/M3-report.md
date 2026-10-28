# M3 Report: Nix Package Manager on Asterinas RISC-V

> 2026-08-13. Corresponds to M3 of
> `docs/superpowers/plans/2026-08-13-nixos-riscv-track.md`.
> Conclusion up front: **the prebuilt riscv64 musl `nix` binary runs on the
> Asterinas kernel — `nix --version` prints `nix (Nix) 2.31.5`.** Nix's full
> dynamic dependency closure (45 Alpine packages, musl ABI) loads and
> initializes. `nix eval` is not yet reachable: Nix spawns a thread on startup
> and that thread faults at thread-control-block offset 8 — a kernel gap in
> thread/TLS handling documented below.

## Deliverables (tools/riscv/nixos/m3/)

| File | Purpose |
|---|---|
| `resolve_deps.py` | Walks Alpine APKINDEX metadata to compute Nix's install closure |
| `build_m3.sh` | Downloads + extracts the 45 `.apk`s into `target/nixos/m3/rootfs`, assembles the initramfs |
| `init_m3.c` | `/init`: mounts /proc /sys /tmp, prepares `/nix`, seeds env, runs the smoke script |
| `boot_m3_smoke.py` | QEMU boot driver: U-Boot booti handoff, serial marker checks |
| `M3-report.md` | This report |

Artifacts (`target/nixos/m3/`): `m3-initramfs.cpio.gz` (raw newc cpio, ~36 MB),
`rootfs/` (the assembled Nix rootfs), `closure.json` (the dependency graph).

## Approach: Alpine edge prebuilt riscv64 Nix (no source compile)

Instead of cross-compiling Nix from source, we reuse Alpine's prebuilt riscv64
musl `nix` package. Alpine is musl-based, matching the M2 toolchain target, and
its riscv64 port is served by the TUNA mirror (direct, proxy-free — see
`../MIRRORS.md`).

- `nix` is **not** in Alpine v3.22 main/community for riscv64, but is in
  **edge** community: `nix-2.31.5-r1.apk` (2.31.5, riscv64, musl).
- `resolve_deps.py` parses the edge `APKINDEX` files and resolves the full
  closure from the `D:`/`p:` dependency/provides fields.

### Dependency closure (45 packages)

nix links `libnix{util,store,expr,fetchers,flake,main,cmd}.so` plus:

- **runtime libs**: `libgc` (Boehm), `libstdc++`, `libgcc_s`, `musl` (libc)
- **store/archive**: `libarchive` (acl, bz2, expat, lz4, xz, zlib, zstd), `libsqlite3`
- **fetch/net**: `libcurl` (c-ares, libidn2, libpsl, libssh2, libunistring, nghttp2), `libgit2` (llhttp, pcre2), `libssl3`/`libcrypto3` (OpenSSL)
- **content hashing**: `libblake3` → `onetbb` → `hwloc` → `libxml2`/`eudev-libs`
- **Nix internals**: `libsodium`, `libseccomp`, `editline`, `lowdown-libs`, `brotli`
- **boost**: `boost1.84-context/-iostreams/-url` (1.84.0)
- `busybox` (/bin/sh), `ca-certificates` (+ bundle)

Full list is in `closure.json`. Test-only binaries (`nix-*-tests`,
`gtest`/`gmock`/`rapidcheck`) are pruned — the `nix` CLI never links them and
they add ~9 MB (see `build_m3.sh` step 4b).

## Reproduction

```bash
# 1. Build the Nix rootfs + initramfs (downloads ~45 .apk from TUNA Alpine edge)
tools/riscv/nixos/m3/build_m3.sh

# 2. Re-pack the boot disk (U-Boot binary, kernel Image, and DTB are already
#    cached in target/qemu-uboot/; only the initramfs payload is swapped)
STAGE=target/qemu-uboot/current/.m3-stage
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp target/osdk/aster-kernel-osdk-bin.Image "$STAGE/asterinas.booti"
cp target/nixos/m3/m3-initramfs.cpio.gz "$STAGE/initramfs.cpio.gz"
cp target/qemu-uboot/current/qemu-virt.dtb "$STAGE/qemu-virt.dtb"
rm -f target/qemu-uboot/current/boot.ext4
truncate -s 96M target/qemu-uboot/current/boot.ext4
mkfs.ext4 -q -F -d "$STAGE" target/qemu-uboot/current/boot.ext4
rm -rf "$STAGE"

# 3. QEMU smoke
python3 tools/riscv/nixos/m3/boot_m3_smoke.py
```

The kernel Image and U-Boot binary are reused unchanged (same HEAD as M1/M2);
this tree only builds userspace.

## Results

`nix --version` prints its banner; the smoke driver reports:

```
=== M3 smoke results ===
  nix --version: OK            # "nix (Nix) 2.31.5"
  nix --version exits cleanly: MISSING
  thread gap (page fault @0x8): OK
  membarrier gap (syscall 283): OK
  nix eval -> 2: MISSING
```

The `nix` binary execs as a PIE, the musl loader resolves the full `.so`
closure, and Nix's C++ runtime (libstdc++/Boost/Boehm-GC/OpenSSL/SQLite/
libcurl/libarchive/libblake3) all initialize far enough to print the version.
This is the M3 acceptance criterion `nix --version`.

## Syscall / kernel gap list

The serial log records these gaps (the `nix` startup path exercises them):

| Signal | Detail | Impact |
|---|---|---|
| `Unimplemented syscall 283` | `membarrier` (ENOSYS) | called by libstdc++ during thread setup; harmless alone |
| `Unimplemented syscall 258` | `riscv_hwprobe` (ENOSYS) | glibc startup probe, harmless (inherited from M1) |
| `Unimplemented syscall 293` | `rseq` (ENOSYS) | glibc startup probe, harmless (inherited from M1) |
| `clone` flags | `CLONE_SYSVSEM` (warned), `CLONE_DETACHED` (unsupported) | ignored by kernel; not fatal |
| **page fault @0x8 (R\|W)** | new thread faults writing to TCB offset 8 | **fatal to threads** — see below |

### Blocking gap: thread creation faults at TCB offset 8

`nix --version` initializes Nix, which starts a thread (Boehm-GC mark thread /
signal-handler thread) via `clone(CLONE_THREAD|CLONE_SETTLS|…)`. The child
thread's first TLS write goes to virtual address `0x8` — the thread pointer
(`tp`) register is `0` in the child, so `tp[1]` (the DTV slot of musl's TCB)
resolves to `NULL+8`. The kernel's page-fault handler reports

```
page fault handler failed: PageFaultInfo { address: 0x8, required_perms: READ|WRITE }
  err: EACCES "no VM mappings contain the page fault address"
```

and the fault is not turned into a `SIGSEGV` that kills the thread; instead the
faulting thread loops and the process never reaches `exit_group`. Consequence:
`nix --version` prints its banner but does not exit, so the subsequent
`nix eval` in the smoke script is never reached.

`kernel/src/process/clone.rs` does implement `CLONE_SETTLS` (it calls
`child_context.set_tls_pointer(tls)`), so the likely defect is in the RISC-V
`tp`-register handling of the cloned user context, or in delivering `SIGSEGV`
for an unmapped user fault. This is a **kernel-side** fix for the sibling
`asterinas-riscv` tree, not a userspace issue.

## Other findings (userspace build side)

1. **gzip initramfs decode is unreliable at >16 MB.** The kernel's
   `zune_inflate::decode_gzip` hangs non-deterministically on the ~18.8 MB
   gzip'd rootfs (sometimes before `[kernel] unpacking initramfs.cpio.gz`).
   Switched to an uncompressed newc cpio (~36 MB), which decodes directly and
   boots reliably. `build_m3.sh` therefore emits raw cpio.
2. **`/dev /proc /sys /tmp` must exist in the rootfs.** Without them, the
   kernel's first-process stdio setup (`open /dev/console`) panics with
   `ENOENT "path resolution did not reach the final target"`. M1/M2 created
   these explicitly; `build_m3.sh` now does too.
3. **`.apk` = gzip-wrapped tar.** Each package is a single `tar -xzf`
   away; `resolve_deps.py` + `build_m3.sh` reproduce what apk-tools would
   install, minus triggers (busybox applet symlinks and `/etc/passwd` are
   recreated by hand).

## Next steps (M4)

- Fix the clone/`tp`-register + user-fault→`SIGSEGV` gap in the kernel, then
  re-test `nix eval` and `nix-store` operations.
- `nix eval nixpkgs#hello.name` additionally needs a writable `/nix/store`,
  `AF_UNIX` for the daemon, and network fetch of a nixpkgs flake (or a bundled
  expression).
