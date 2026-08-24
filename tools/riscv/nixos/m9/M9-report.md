# M9 Report: lightweight NixOS demo — Route A landed (issue #30)

> 2026-08-14. Follow-up to `M8-report.md`. Goal: turn the Route-A prototype
> into a **demo-grade lightweight NixOS** — a real PID-1 system with a
> getty/login loop, minimal service management, and a prebuilt closure of
> common software installed through `nix profile`.
>
> **Conclusion up front: done.** The system boots into a `getty`/`login` loop
> (root / `nixos`), installs a **two-generation Nix profile** at boot, starts
> three services (one of which is itself a Nix-installed daemon), and lets you
> log in and run six Nix-installed binaries by bare name — including **real
> curl 8.21.0 and jq 1.8.2**. The full acceptance sequence
> (boot → login → run multiple Nix-installed binaries → services up) passes
> **8/8 checks** in QEMU with **no kernel change**.

## TL;DR

| Check | Status |
|---|---|
| `/init` mounts `/proc` `/sys` `/tmp` `/run`, sets hostname | ✅ |
| busybox `init` + `/etc/inittab` (sysinit rc + getty respawn) | ✅ |
| getty → login → `-sh` → `/etc/profile` activation | ✅ |
| 2-generation `nix profile install` (core tools + real curl/jq) | ✅ |
| 6 Nix-installed binaries run by bare name | ✅ |
| services up (syslogd, crond, nix-managed heartbeat) | ✅ |
| getty respawns after logout (login loop) | ✅ |

## 1. Scope and approach

M8 concluded **Route A is GO** and recommended three follow-ons: (1) make the
login shell a persistent PID-1 child (a real getty/login loop); (2) install a
second package to prove profile generations; (3) drive it `-smp 4` once the
virtio-blk race is fixed. M9 delivers (1) and (2), plus the milestone's own
requirements — minimal service management and a set of common software — and
defers (3) as the race is unchanged (see §6).

The design is the Alpine-style system M8 described, expanded:

- **PID 1 is busybox `init`.** A small static `/init` does the early work
  (pseudo-FS mounts, hostname, `/nix` prep, environment), then `exec`s
  `busybox init`, which becomes PID 1 and reads `/etc/inittab`.
- **Service management is `rc` + `/etc/init.d/S*`.** `::sysinit:/etc/rc` runs
  the boot script synchronously (install profile, start services); a separate
  `S30heartbeat` service is a **Nix-derivation-driven** daemon.
- **Software is a *prebuilt closure*.** Nothing is compiled in the guest. The
  four core tools are cross-compiled on the host with `riscv64-linux-musl-gcc`;
  curl and jq are real riscv64 packages extracted from Alpine APKs.

## 2. What was built

### 2.1 `/init` and the init system

`init_m9.c` (static glibc, the M1–M8 pattern) opens `/dev/console`, mounts
`proc`/`sysfs`/`tmpfs`/`tmp`/`run`, sets `hostname = nixos-riscv`, creates the
`/nix` layout and baseline environment, then `execv("/bin/busybox", ["init"])`.

`/etc/inittab`:

```
::sysinit:/etc/rc
ttyS0::respawn:/sbin/getty -L 38400 ttyS0 vt100
::ctrlaltdel:/sbin/reboot
::shutdown:/etc/rc.shutdown
::restart:/sbin/init
```

busybox `init` runs `::sysinit` synchronously (so the profile is installed and
the services are started *before* `getty` offers a login), then respawns
`getty` on `ttyS0` forever.

### 2.2 Profile activation

`/etc/profile` is the entire activation, exactly as in M8 but polished:

```sh
export PATH="/nix/var/nix/profiles/default/bin:/root/.nix-profile/bin:$PATH"
export NIX_PROFILES="/nix/var/nix/profiles/default /root/.nix-profile"
```

`login` execs the shell as `-sh`, busybox ash sources `/etc/profile`, and every
Nix-installed binary is callable by bare name.

### 2.3 Service management

`/etc/rc` runs `/etc/init.d/S*` in order:

| Service | Daemon | Proves |
|---|---|---|
| `S10syslogd` | busybox `syslogd -n -O /var/log/messages` | logging |
| `S20crond` | busybox `crond -f` | scheduling |
| `S30heartbeat` | `/nix/var/nix/profiles/default/bin/heartbeat` | a **Nix-managed service** |

`heartbeat` is the load-bearing one: the daemon binary is the *output of
`core.nix`*, installed into `/nix/store` and started through the profile. It
appends a line to `/var/log/heartbeat.log` every 2 s, so "the service is up" is
directly observable.

### 2.4 The prebuilt closure of common software

Two `nix profile install`s produce two generations:

| Generation | Derivation | Binaries | Source |
|---|---|---|---|
| 1 | `core.nix` | `hello`, `nixos-info`, `fortune`, `heartbeat` | cross-compiled here (musl-gcc) |
| 2 | `real.nix` | `curl 8.21.0`, `jq 1.8.2` | Alpine riscv64 APKs |

The derivations are `builtins.derivation`s whose builder is `/bin/sh` copying a
prebuilt binary into `$out/bin` — the "path B" from M6/M8. curl's shared
library closure (libcurl, libssl, libz, libnghttp2, …) is already present in
the base image `/usr/lib` from nix itself; jq's `libjq.so.1` and `libonig.so.5`
are added to `/usr/lib` by the build script. `nixos-info` reports the result:
**6 store paths, 2 profile generations**.

## 3. Kernel capability notes

No kernel change was needed. The one capability M8 had *not* yet exercised for
an interactive login — the **TTY/termios stack** — was verified present before
writing any code:

- `TCGETS`/`TCSETS` (`0x5401`/`0x5402`) and `TIOCGWINSZ`/`TIOCSWINSZ`
  (`kernel/src/device/tty/ioctl_defs.rs`), dispatched in
  `kernel/src/device/tty/mod.rs`.
- `TIOCSCTTY` (`0x540E`), `TIOCGPGRP`/`TIOCSPGRP`, `TIOCNOTTY` — job-control
  ioctls in `kernel/src/process/process/terminal.rs`.
- A full line discipline (`line_discipline.rs`): canonical mode, `ECHO`,
  `ICRNL`, and signal chars (`VINTR`/`VQUIT`/`VEOF`/`VKILL`).

This is the same TTY stack upstream Asterinas uses to boot a x86_64 login, and
it worked unchanged on RISC-V — `getty`'s `setsid` + `TIOCSCTTY` + `tcsetattr`
and `login`'s echo-suppressed password prompt both behaved.

## 4. Results

`boot_m9_smoke.py` boots, drives U-Boot, waits for `rc`, logs in, runs the
binaries, checks the services, logs out, and confirms `getty` respawns.

```
=== M9 lightweight-NixOS smoke results ===
  hello: OK
  nixos-info: OK
  fortune: OK
  curl --version: OK
  jq --version: OK
  heartbeat service: OK
  services running: OK
  getty respawn after logout: OK
```

Representative serial output (with `loglevel=warn` — see §6):

```
  hostname : nixos-riscv
  kernel   : 5.13.0
  arch     : riscv64
  uptime   : 31.19 0.08s
  MemTotal:        2049028 kB
  nix      : 6 store paths, 2 profile generations
NIXOS# curl --version
curl 8.21.0 (riscv64-alpine-linux-musl) libcurl/8.21.0 OpenSSL/3.5.7 zlib/1.3.2
...
NIXOS# jq --version
jq-1.8.2
NIXOS# for p in syslogd crond heartbeat; do echo __M9_SVC_${p}__=$(pidof $p ...); done
__M9_SVC_syslogd__=OK
__M9_SVC_crond__=OK
__M9_SVC_heartbeat__=OK
NIXOS# exit
nixos-riscv login:          # getty respawned — the login loop
```

## 5. Decisions worth recording

1. **Single-user nix, no daemon.** M8 found daemon-mode `nix profile install`
   hangs while single-user completes in ~25 s. M9 stays single-user
   (`build-users-group =` in `nix.conf`). Two installs ≈ 60 s of boot.
2. **`loglevel=warn`.** The inherited `loglevel=info` boot arg turns every
   syscall into a serial `INFO` line, drowning the demo output. The smoke now
   boots with `loglevel=warn`, which Asterinas honours (its cmdline component
   parses `loglevel=`); the demo output is clean, with only two harmless
   `WARN`s (`riscv_hwprobe` ENOSYS and a TTY-steal warning on logout).
3. **`crond -f &` not `crond -b`.** The first cut used `crond -b`
   (self-daemonize); `pidof crond` then reported MISSING. Running it foreground
   and backgrounding via the shell (same as the other services) fixed it — a
   busybox invocation detail, not a kernel gap.

## 6. Remaining gaps (all inherited, none blocks M9)

| Gap | Impact |
|---|---|
| `ET_EXEC` + `PT_INTERP` ELF-loader bug | blocks non-PIE dynamic binaries (`gcc`); forces the prebuilt-closure approach. **Candidate follow-up: a kernel fix worth PR-ing to `main`** (root-caused in M6, re-confirmed M8) |
| virtio-blk SMP race | `-smp 4` hangs ~2/3 boots; M9 stays `-smp 1` |
| `devtmpfs` not a mountable fstype | `/dev` nodes come from the registry; no `/dev` mount needed |
| seccomp BPF unimplemented | bypassed with `filter-syscalls = false` |
| `riscv_hwprobe`(258)/`membarrier`(283)/`rseq`(293) ENOSYS | harmless startup probes |
| `pivot_root` from initramfs root → EINVAL | irrelevant here (no `switch_root`; busybox init runs on the initramfs root) |

## 7. Optional: ext2 persistence of `/nix/store` (attempted, blocked by kernel)

The bonus deliverable — persisting `/nix/store` on a second `virtio-blk` disk
so it survives reboot — was attempted and is **blocked by a kernel-side issue**,
not by any user-space problem:

- The kernel's ext2 driver is registered (`fs_impls/ext2/fs_type.rs`, name
  `"ext2"`), and a second virtio-blk disk appears as `/dev/vdb`
  (`virtio/src/device/block/device.rs` names devices `vda`, `vdb`, …).
- `make_persist_disk.sh` creates a 256 MiB ext2 image with **4096-byte blocks**
  (the driver rejects any other block size — `fs.rs` checks
  `log_block_size == 2`, and mke2fs otherwise picks 1024 for a small volume).
- `/init` (init_m9.c) `stat`s `/dev/vdb` and, if present, mounts it on `/nix`
  before `rc` runs nix.

Result: `/dev/vdb` exists and the on-disk superblock magic (`0xef53`) is
readable through it, but `mount -t ext2 /dev/vdb /nix` returns `EINVAL`
(`"unsupported block size"` / superblock validation), and a raw `read` of the
block device returns inconsistent bytes (correct magic at offset 1080, wrong
`log_block_size` at offset 1048). This points at the **block-device read path
for a non-boot virtio-blk disk** (or the ext2 superblock validation against it)
rather than at the image itself, which mounts fine on the host. Root-causing it
needs kernel debugging — out of scope for M9's "no kernel change" mandate — so
it is left as a documented follow-up. The plumbing (`make_persist_disk.sh`,
`boot_m9_persist_smoke.py`, and the `/init` mount attempt) is kept in-tree as a
starting point. It does not block the acceptance criteria.

## 8. Reproduction

```bash
tools/riscv/nixos/m9/build_m9.sh
python3 tools/riscv/nixos/m9/boot_m9_smoke.py
```

See `README.md` for the hand-driven demo (login `root` / `nixos`).

## 9. Files changed

- `tools/riscv/nixos/m9/` — `init_m9.c`, `inittab`, `rc`, `rc.shutdown`,
  `motd`, `profile`, `passwd`, `group`, `shadow`, `securetty`,
  `init.d/{S10syslogd,S20crond,S30heartbeat}`, `core.nix`, `real.nix`,
  `tools/{hello,nixos_info,fortune,heartbeat}.c`, `build_m9.sh`,
  `boot_m9_smoke.py`, `README.md`, `M9-report.md`. No kernel source changes.
