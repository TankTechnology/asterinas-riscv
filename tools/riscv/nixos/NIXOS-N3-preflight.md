# NIXOS-N3 preflight — nix-daemon feasibility on Asterinas riscv64

Date: 2026-08-23
Branch: `track/nixos` (post main-merge @ `5c11d1329`)
Scope: reconnaissance only. No implementation.

---

## 1. Nix binary acquisition (verified against live services)

**The official Nix release tarballs ship riscv64-linux.** Verified by direct
HEAD/GET probes (bogus names correctly return 404, so the 200s are real):

| URL pattern | Status |
|---|---|
| `releases.nixos.org/nix/nix-{2.24.14,2.28.5,2.30.2,2.31.2}/nix-<v>-riscv64-linux.tar.xz` | all 200, real payloads |
| `nix-2.30.2-riscv64-linux.tar.xz` | downloaded (21 MB), listing inspected |

The tarball is a **self-contained `/nix/store` closure** (44 store paths):
`nix-2.30.2` (with `bin/nix`; `bin/nix-daemon` is a symlink to `nix`), its own
`glibc-2.40` riscv64 (interpreter
`/nix/store/1ayh...-glibc-...-2.40-66/lib/ld-linux-riscv64-lp64d.so.1`),
`sqlite`, `curl`, `nss-cacert`, `libseccomp`, plus a riscv64 `busybox-1.36.1`
(the sandbox shell). Nothing needs to be cross-compiled by us.

**Binary caches** (probed 2026-08-23):

- `cache.nixos.org` — **does carry the nix riscv64 closure**: narinfo for the
  nix package itself, its glibc, and the sandbox busybox all return 200. This
  is because the Nix project's own CI pushes riscv64 builds to the official
  cache. General nixpkgs riscv64-linux coverage is *not* guaranteed (Hydra has
  no riscv64-linux release jobset; hydra.nixos.org was unreachable from the
  host at probe time, could not confirm directly).
- `riscv64.cachix.org` — live (`nix-cache-info` 200) but does not have these
  paths (404). A community cache to revisit if we need a wider package set.
- Fallback: the release tarball makes the daemon bootstrappable offline; for
  building derivations we can start with `substitute = false` (local builds)
  and evaluate cache coverage later.

Guest integration path: unpack the tarball's `store/` into the guest's
`/nix/store` (ext4 data disk or an enlarged initramfs), add the `nix` profile
to PATH, point `/etc/resolv.conf` at slirp's DNS (10.0.2.3) when substituters
are enabled.

## 2. Kernel requirement checklist vs. current state (post main-merge)

| Requirement | Used by | State | Evidence |
|---|---|---|---|
| AF_UNIX stream + `SCM_RIGHTS` | daemon protocol (fd passing to builders) | **have** | verified in M7 (`tools/riscv/nixos/m7/M7-report.md`) |
| AF_NETLINK route/uevent | systemd integration, udev | **have** | N1 gate 9/9 (this week) |
| `signalfd4` | nix main loop | **have** | wired, `syscall/arch/generic.rs:296` |
| `eventfd2`, `timerfd_*`, `epoll` | event loop | **have** | syscall table |
| `fcntl` POSIX locks (F_GETLK/SETLK/SETLKW), `flock` | SQLite store DB locking | **have** | `syscall/fcntl.rs:30-32`, `syscall/flock.rs` |
| `clone3`/`clone`, `rseq`, `set_robust_list`, `statx`, `openat2`, `getrandom` | glibc 2.40 baseline | **have** | syscall table (rseq landed in M8) |
| user namespaces (`CLONE_NEWUSER`, uid/gid maps, ns-aware capabilities) | sandbox privilege model | **have (stage 1)** | main `21c427583` + `2c65b6e90` (writable uid_map/gid_map/setgroups) |
| PID namespaces (`CLONE_NEWPID`, ns-aware getpid/kill/wait4/procfs) | sandbox isolation | **have (new)** | main `21d5e6612`, `a8a435cb5`, guest-tested `d4a2f54e7` |
| mount ns + `MS_PRIVATE\|MS_REC`, bind mounts, remount-ro, `pivot_root` | sandbox root setup | **have, hardening TBD** | design doc §"mount/ipc/uts"; `pivot_root` fixed in main `2676fe6bf` |
| UTS/IPC namespaces | sandbox | **have** | `uts_ns.rs`, `ipc_ns.rs` (ipc permission checks TODO — OK for builds) |
| cgroup v2 `memory.max`/`memory.high` | resource limits | **have** | main `82e35363f` |
| **`CLONE_NEWNET`** (loopback-only net ns) | **default sandbox** (`clone(...|CLONE_NEWNET|...)`) | **missing** | still EINVAL, `nsproxy.rs:240-247` |
| `recvmmsg`, `statmount`, `cachestat`, `futex_wake` | — | missing | not wired; need evidence any nix path actually calls them |
| socket ioctls `SIOCGIF*` | not used by nix | missing | cosmetic gap from N1 |

**Blocker analysis.** nix-daemon itself (store DB, client sessions,
non-sandboxed builds) needs nothing we lack — the remaining hard gap is
`CLONE_NEWNET`, which is part of the *default* sandbox clone. Options:

1. B-track implements a minimal loopback-only `NetNamespace` (design doc §net;
   "the sandbox never asks for connectivity, only for isolation").
2. Interim: run builds with `sandbox = false` in the guest's nix.conf (nix
   daemon fully works, builds run unsandboxed — acceptable for bring-up).

## 3. N3 card decomposition proposal

| Card | Content | Est. | Depends on |
|---|---|---|---|
| N3-1 | Guest packaging: unpack the 2.30.2 closure onto a private ext4 data disk / enlarged initramfs; `nix --version`, `nix-store --version` smoke | 0.5 d | — |
| N3-2 | Daemon bring-up: `/nix/var` layout, `nix-daemon` startup, unix socket at `/nix/var/nix/daemon-socket/socket`, `nix store ping --daemon`; chase glibc 2.40 syscall stragglers | 0.5–1 d | N3-1 |
| N3-3 | Client→daemon session: `nix store add` / trivial fixed-output-less build with `sandbox = false`; SQLite store DB lock exercise | 1 d | N3-2 |
| N3-4 | Sandboxed trivial build (`sandbox = true`) | 0.5–1 d | **B-track `CLONE_NEWNET`** (or documented `sandbox=false` fallback) |
| N3-5 | Substituters: slirp DNS (10.0.2.3), TLS via bundled nss-cacert, `cache.nixos.org` fetch of a riscv64 path (the nix closure itself is a self-test) | 0.5–1 d | N3-2, virtio-net (have) |

Total: **~3–4.5 days**, with N3-4's critical path owned by the B-track
net-namespace card. N3-1..N3-3 can start immediately and cover the
"nix-daemon is usable" milestone without the sandbox.

## 4. Risks / open questions

- glibc 2.40 on a 5.13-versioned kernel: glibc's minimum kernel for riscv64 is
  4.15 (ELF note confirms), so no baseline conflict; but individual newer
  syscalls (statmount/cachestat/recvmmsg) may surface at runtime as ENOSYS —
  mitigated by N3-2's strace-style bring-up.
- SQLite WAL mode uses `mmap` + POSIX locks; both exist but WAL in nix's DB
  has never been exercised under Asterinas — watch for lock-fork interplay.
- General nixpkgs riscv64 cache coverage unknown; if thin, `nix build
  nixpkgs#hello` means building stdenv from source in-guest — heavy but not
  blocked (busybox sandbox shell is already in the closure).
- `CLONE_NEWTIME` also EINVAL; nix doesn't use it. No action.
