# NIXOS-N3 — nix-daemon bring-up: official glibc Nix 2.30.2 in the riscv64 guest

Date: 2026-08-23
Branch: `track/nixos` (on top of the main-merge `5c11d1329`)
Commits:
- `2b174a8b9` test(nixos): NIXOS-N3 guest gate — glibc Nix 2.30.2 closure, daemon, first build
- (netprobe + substituter ping, this report)
Status: **N3-1/N3-2/N3-3 complete; N3-5 core proven (HTTPS substituter reachable); N3-4 blocked on CLONE_NEWNET (B-track).**

---

## 1. What runs in the guest now

Gate: `tools/riscv/nixos/n3/` (`build_n3.sh` → raw uncompressed initramfs with
the official `nix-2.30.2-riscv64-linux` closure; `boot_n3_smoke.py` drives the
headless QEMU boot from a private `/tmp/n3-nix/boot.ext4`). The closure
tarball (21 MB, 45 store paths, self-contained glibc 2.40 + busybox sandbox
shell) is backed up at `~/Program/backups/nix-riscv64/`.

Guest transcript (all 7 checks green):

```
nix (Nix) 2.30.2                       # N3-1: glibc-dynamic nix runs
nix-store (Nix) 2.30.2
nix-store --load-db < /nix/.reginfo    # rc=0 — closure registered as valid
NIX_REMOTE=daemon nix store ping       # N3-2: Store URL: daemon / Version: 2.30.2 / Trusted: 1
NIX_REMOTE=daemon nix profile add ...-busybox-1.36.1   # N3-3: exit 0, profile populated
/nix/var/nix/profiles/default/bin/ -> busybox, ash, sh # profile binary executes
nix-instantiate trivial derivation + NIX_REMOTE=daemon nix-store --realise
  building '/nix/store/8f9dnf4sic7s899pnfrakvpwya0gjci9-n3-hello.drv'...
  /nix/store/4namh6905i9mhrvk28byaw04qxy4js6j-n3-hello -> "hello-from-n3"
nix store ping --store https://cache.nixos.org   # N3-5 core: exit 0 over HTTPS
```

Note the daemon-mode profile install **does not hang** with the glibc build —
the M8 hang ("daemon accepted the client and looped on openat/read/fcntl")
was specific to the musl Nix 2.31.5 rootfs.

## 2. Kernel gaps found (A-track output)

| Gap | Symptom | Status |
|---|---|---|
| **UDP inbound delivery** | DNS query to slirp (10.0.2.3:53) sends fine (`sendto`=33) but the reply is never delivered (`recv` times out, EAGAIN); TCP works (RST from 10.0.2.2:80 arrives, HTTPS to cache.nixos.org completes). Blocks real DNS; worked around with `/etc/hosts` | **new kernel card** (not fixed here) |
| `CLONE_NEWNET` | still EINVAL (`nsproxy.rs`) — blocks the default nix build sandbox | B-track; `sandbox = false` fallback works |
| socket ioctls `SIOCGIF*` | cosmetic `ip` noise (from N1) | open |

Everything nix-daemon actually needed was already present post main-merge
(userns/pidns, netlink from N1, SCM_RIGHTS from M7, fcntl locks for SQLite).
No kernel patch was required for N3-1..N3-3 — glibc 2.40's baseline
(clone3/rseq/statx/openat2) is fully wired. The preflight watchlist
(statmount/cachestat/recvmmsg) never fired: nix 2.30.2 does not call them on
these paths.

## 3. Tooling gotchas worth remembering

- The install tarball's DB bootstrap command is `nix-store --load-db <
  .reginfo`, **not** `--register-validity` (which fails confusingly with
  "not an absolute path" on the same file).
- The cpio must stay **uncompressed** (zune-inflate hangs on >16 MB gzip,
  M3-report.md); 75 MB initramfs boots fine from a 256 MB private disk.
- Busybox `CONFIG_TEST1` (`[`) and `head -n N` quirks: use `test` and
  `head -n` in init scripts, and beware that `cmd | head` masks cmd's exit
  code (burned once on `--load-db` diagnostics).
- `/etc/nsswitch.conf` (`hosts: files dns`) is needed for glibc name service
  to consult `/etc/hosts` at all.

## 4. Remaining work (N3-4/N3-5)

- **N3-4 sandboxed build**: needs B-track `CLONE_NEWNET` (minimal
  loopback-only net namespace). Then flip `sandbox = true` and re-run the
  gate's derivation build.
- **N3-5 full substitution**: proven end-to-end for `nix store ping`; a real
  `nix-store --realise` with substitution needs (a) the UDP/DNS fix or the
  hosts workaround generalized, and (b) a riscv64 target path on
  cache.nixos.org to fetch (nixpkgs riscv64 coverage is thin; the nix closure
  itself is confirmed cached).
- Profile generations/updates (`nix profile upgrade`, GC) — natural next gate
  extension, no known blockers.
