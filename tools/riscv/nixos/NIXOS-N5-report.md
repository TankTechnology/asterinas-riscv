# NIXOS-N5 — from hand-assembled rootfs to a persistent, closure-form system

Date: 2026-08-23
Branch: `track/nixos`
Commits:
- (N5 gate assets + this report)
Status: **Complete — stage-1 initramfs → persistent ext2 root → systemd (stage-2) chain verified across a reboot; nix profile survives. Gate 7/7.**

---

## 1. System shape

Two private disks under `/tmp/n5` (the shared boot disk is never touched):

- `boot.ext4` (64 MB, repacked per run): kernel Image + stage-1 initramfs
  (a single 200-line static `/init`) + DTB.
- `root.ext2` (512 MB, **persistent**, created once and reused): the proven
  hand-built systemd tree (systemd 257.5 riscv64 from the B-track
  cross-build, glibc 2.41 runtime, busybox helpers) **plus** the official
  `nix-2.30.2-riscv64-linux` closure at `/nix/store` (45 paths, from the
  backed-up release tarball) with `.reginfo`, `nix.conf`
  (`sandbox = false`, `substituters = https://cache.nixos.org`,
  `require-sigs = false`), NSS/resolver config.

ext2 because Asterinas has an ext2 filesystem implementation (no ext4).

## 2. Boot chain (verified in QEMU, two boots off the same disks)

1. **Stage-1** (`tools/riscv/nixos/n5/init_stage1.c`, initramfs PID 1):
   mounts `/dev/vdb` (the ext2 disk) at `/newroot`, bind-mounts `/dev` into
   it (device nodes come from the kernel registry, not a devtmpfs mount),
   `chroot`s, and runs the stage-2 script.
2. **Stage-2 script** (PID 1 on the persistent root): mounts proc/sys/tmp,
   then the R1-B persistence check, then `exec /init` — the proven systemd
   launcher on the disk, which exec()s `systemd` as PID 1.

Boot 1 (fresh disk):

```
__N5_STAGE1_OK__
__N5_FIRST_BOOT__
nix-store --load-db  -> rc=0
nix-daemon up; NIX_REMOTE=daemon nix profile add ...busybox -> __N5_INSTALL_RC__=0
__N5_STAGE2_SYSTEMD__
systemd 257.5 running in system mode ... Reached target Multi-User System
Startup finished ... login prompt
```

Boot 2 (same disk, no reinstall):

```
__N5_STAGE1_OK__
__N5_PROFILE_PERSISTED__
__N5_PROFILE_RUNS__            # profile's busybox sh executes from disk
__N5_STAGE2_SYSTEMD__
systemd 257.5 running in system mode ...
```

Gate: `tools/riscv/nixos/n5/{init_stage1.c,build_n5.sh,boot_n5_smoke.py}`
— 7/7 checks green.

## 3. R1-A (reproducible closure) — path chosen and differences vs the codex-line R1 plan

The host has no real nix (only a `/usr/local/bin/nix-build` stub), so a
host-side `nixpkgs` NixOS toplevel eval is not available. The path taken:

- the **official Nix release closure** is the reproducible input (fixed
  URL, fixed hashes, contents verified against cache.nixos.org in N3/N4);
- `nix-store --load-db` + a daemon-mode `nix profile add` materialize and
  register the closure inside the guest;
- the systemd userspace is still the hand-built tree from the M-series
  (cross-compiled in the sibling tree), *not* a nixpkgs-built closure.

Differences vs the R1-A/R1-B planning semantics: this achieves R1-B
(persistent root, reboot-stable state) fully, and R1-A only in the weaker
sense of "a reproducibly-sourced /nix/store closure coexists with the
system". A true nixpkgs `toplevel` (bash/coreutils/etc as evaluated
store paths plus an activation script) still needs either a host-side nix
(e.g. nix-portable) for eval + riscv64 package availability, or a riscv64
bootstrap cache — neither exists today. That remains the next card's
build-side problem, not a kernel problem.

## 4. Gaps hit (all minor)

- The nix-closure busybox ships only `ash`/`sh` applets — calling
  `busybox echo` prints "applet not found"; run it as `sh -c` instead.
  (Wasted one gate cycle.)
- `mkfs.ext2 -d` of the 135 MB staging tree into the 512 MB image and the
  chroot/bind-mount/ext2-rw chain all worked on the first try — **no kernel
  gap was hit in this card**. The `switch_root`-style chroot handoff M8
  predicted as the viable route is confirmed end-to-end.

## 5. Regenerating the disks

```
tools/riscv/nixos/n5/build_n5.sh        # recreates /tmp/n5/{boot.ext4,root.ext2}
python3 tools/riscv/nixos/n5/boot_n5_smoke.py
```

`root.ext2` is derivable from `target/nixos/systemd/rootfs` +
`~/Program/backups/nix-riscv64/nix-2.30.2-riscv64-linux.tar.xz`, so the
image itself is not backed up.

## 6. Next steps

- R1-A proper: host-side nix (nix-portable) or guest-side eval of a minimal
  nixpkgs toplevel; needs riscv64 nixpkgs package availability assessed
  first (cache coverage is thin beyond the nix closure itself).
- Wire the nix-daemon as a systemd unit in stage-2 (today it runs only in
  the stage-2 pre-systemd script).
- Real NixOS activation semantics (users/groups, /etc population) once a
  nixpkgs toplevel exists.
- CLONE_NEWNET (B-track) to enable `sandbox = true` builds in the guest.
