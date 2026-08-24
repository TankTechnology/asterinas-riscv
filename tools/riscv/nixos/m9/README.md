# M9 — A lightweight NixOS demo on Asterinas RISC-V

This milestone turns the Route-A prototype from M8 into a **demo-grade
lightweight NixOS**: a real PID-1 system (busybox `init`) that boots into a
`getty`/`login` loop, installs a **two-generation Nix profile** at boot, starts
a small set of services (including one that is itself a Nix-installed daemon),
and lets you log in and run Nix-installed binaries by bare name.

No systemd, no `switch_root`, no `nix-daemon` — exactly the Alpine-style
"busybox init + nix profile activation" system that M8 recommended as **GO**.

```
      ___     _         _
     / _ \   | |       (_)
    / /_\ \__| |_ ___ _ _ __ _ _ __   ___
    |  _  / __| __/ _ \ | '__| | '_ \ / __|
    | | | \__ \ ||  __/ | |  | | | | |\__ \
    \_| |_/___/\__\___|_|_|  |_|_| |_|___/

  hostname : nixos-riscv
  kernel   : 5.13.0
  arch     : riscv64
  nix      : 6 store paths, 2 profile generations
```

## What it does

1. **`/init`** (static C, `init_m9.c`) — mounts `/proc` `/sys` `/tmp` `/run`,
   sets the hostname to `nixos-riscv`, prepares `/nix`, then `exec`s
   `busybox init` as the new PID 1.
2. **`busybox init`** reads `/etc/inittab`:
   - `::sysinit:/etc/rc` — runs the boot script synchronously.
   - `ttyS0::respawn:/sbin/getty -L 38400 ttyS0 vt100` — the login loop.
3. **`/etc/rc`** installs the Nix profile in two generations, then starts the
   services (`/etc/init.d/S*`), then prints the motd.
4. **Login** — `getty` → `login` (root, password `nixos`) → `-sh`, which
   sources `/etc/profile` (the "activation": profile `bin` on `PATH`).

## The Nix profile (two generations)

| Generation | Derivation | Contents |
|---|---|---|
| 1 | `core.nix` | `hello`, `nixos-info`, `fortune`, `heartbeat` (cross-compiled here with `riscv64-linux-musl-gcc`) |
| 2 | `real.nix` | `curl 8.21.0`, `jq 1.8.2` (a **prebuilt closure** fetched from the Alpine riscv64 mirror) |

Everything is *prebuilt*: the four core tools are cross-compiled on the host,
and curl/jq are extracted from Alpine riscv64 APKs. Nothing is compiled inside
the guest (`gcc` is still blocked by the `ET_EXEC` + `PT_INTERP` ELF-loader gap,
see `../m6/M6-report.md`).

## Services

`/etc/rc` runs each `/etc/init.d/S*` script:

| Service | What | Notes |
|---|---|---|
| `S10syslogd` | busybox `syslogd` → `/var/log/messages` | |
| `S20crond`  | busybox `crond` | |
| `S30heartbeat` | a Nix-installed daemon → `/var/log/heartbeat.log` | **nix-derivation-driven**: the binary is `core.nix`'s output |

The heartbeat daemon is the load-bearing demonstration of "a Nix-managed
service stays up": it is installed into `/nix/store` by `core.nix` and started
through the profile.

## Build

```bash
# One-time prerequisites (already built for M1-M8):
#   - the Asterinas kernel Image (target/osdk/aster-kernel-osdk-bin.Image)
#   - U-Boot (prepare_qemu_uboot_booti.sh)
#   - riscv64-linux-gnu-gcc and riscv64-linux-musl-gcc

tools/riscv/nixos/m9/build_m9.sh          # assembles rootfs + repacks boot.ext4
tools/riscv/nixos/m9/build_m9.sh --no-repack   # skip the boot-disk repack
```

`build_m9.sh` fetches `curl`, `jq` and `oniguruma` APKs from the TUNA Alpine
mirror (already cached under `target/nixos/m9/apks/` after the first run).

## Boot and demo

```bash
python3 tools/riscv/nixos/m9/boot_m9_smoke.py
```

The smoke test boots, waits for `rc`, logs in as `root` (`nixos`), runs the
Nix-installed binaries, checks the services, logs out, and confirms `getty`
respawns. Exit code 0 = all checks pass.

To drive it by hand, boot with the same QEMU command line (see the smoke
script) and, at the `login:` prompt, use `root` / `nixos`, then try:

```sh
hello                 # Hello, world! (from a Nix-installed binary …)
nixos-info            # system banner + nix store/generation counts
fortune               # a random quip
curl --version        # real curl 8.21.0
jq --version          # real jq 1.8.2
echo '{"a":1}' | jq . # jq actually works
tail /var/log/heartbeat.log   # the nix-managed service is alive
nix profile list --profile /nix/var/nix/profiles/default   # 2 generations
```

## Files

| File | Purpose |
|---|---|
| `init_m9.c` | static `/init` (mounts, hostname, exec busybox init) |
| `inittab`, `rc`, `rc.shutdown`, `motd` | busybox init configuration |
| `init.d/S10syslogd`, `S20crond`, `S30heartbeat` | services |
| `profile`, `passwd`, `group`, `shadow`, `securetty` | login + profile activation |
| `core.nix`, `real.nix` | the two profile derivations |
| `tools/*.c` | cross-compiled core tools |
| `build_m9.sh` | assemble the rootfs, cross-compile, fetch, pack, repack |
| `boot_m9_smoke.py` | interactive QEMU smoke test |

## Optional: ext2 persistence of `/nix/store` (experimental, blocked)

`make_persist_disk.sh` + `boot_m9_persist_smoke.py` + the `/dev/vdb` mount in
`init_m9.c` are the scaffolding for the bonus deliverable (persist `/nix` on a
second virtio-blk ext2 disk). It does **not** yet work: `mount -t ext2
/dev/vdb /nix` returns `EINVAL` — a kernel-side issue in the block-device read
path / ext2 validation, not the image (see `M9-report.md` §7). The core demo is
unaffected.

## Known limitations (inherited from M1-M8)

- `gcc`/`cc1` cannot run in-guest (non-PIE `ET_EXEC` + `PT_INTERP` loader gap) —
  hence everything is prebuilt.
- `-smp 1` only: the virtio-blk SMP race hangs `-smp 4` ~2/3 of boots.
- `devtmpfs` is not a mountable fstype; `/dev` nodes come from the device
  registry, so no `/dev` mount is needed.
- seccomp BPF is bypassed (`filter-syscalls = false`).
