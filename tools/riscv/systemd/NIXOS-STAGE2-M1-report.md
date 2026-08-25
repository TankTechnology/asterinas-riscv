# NIXOS-STAGE2-M1 — a minimal NixOS assembly on top of the systemd desktop

Date: 2026-08-14
Status: **MILESTONE ACHIEVED** — systemd 257.5 reaches `graphical.target` **and**
the nix profile is activated into the systemd environment: five nix-installed
binaries (`hello`, `nixos-info`, `fortune`, `jq`, and real **curl 8.21.0**) run
by bare name inside a systemd service, resolved through
`/nix/var/nix/profiles/default`. No kernel changes.

## Objective

Two prior milestones met in the middle. `SYSTEMD-DESKTOP-M1` (this tree) made
systemd supervise the desktop (`graphical.target` → Xorg + 4 session clients).
Session A's `M9` proved a Nix profile can be activated into a login shell
(`/etc/profile` → `PATH=/nix/var/nix/profiles/default/bin`). This milestone
splices the two: **Nix-profile-installed software is referenced from a *systemd*
environment**, not just a login shell. Concretely it delivers four things:

1. the Nix-style *assembly* — how `nix profile install` products are laid out
   (`/nix/store`, `/nix/var/nix/profiles/default`) and referenced by PATH;
2. a minimal **activation script** (a `switch-to-configuration` subset) that
   links the profile's `bin/` into `/usr/local/bin` *and* exports PATH, and
   generates `/etc/profile` + the systemd default environment;
3. a rootfs in this tree: systemd (mine) + desktop units (mine) + the nix
   profile (session A's products copied over);
4. a QEMU smoke: systemd → `graphical.target` → desktop, and a nix binary runs.

## Deliverables (`tools/riscv/systemd/`)

| File | Purpose |
|---|---|
| `nixos/activate` | the activation script (mini `switch-to-configuration`) |
| `nixos/nix-smoke.sh` | the smoke script: run nix binaries by bare name, emit markers |
| `units/nix-activation.service` | oneshot, runs `/etc/activate` before `graphical.target` |
| `units/nix-smoke.service` | oneshot, runs the smoke, wired to `graphical.target` |
| `units/graphical.target` | +`Wants=nix-activation.service nix-smoke.service` |
| `build_systemd_desktop_nix.sh` | assemble systemd + desktop + nix rootfs, pack cpio |
| `boot_systemd_nixos.py` | QEMU driver (extends `boot_systemd_desktop.py`) + nix markers |
| `gate_nixos.sh` | one-command gate: build → repack → boot → report |
| `build_systemd_desktop.sh` | +`--no-pack` (leave the base rootfs staged for nix layering) |
| `NIXOS-STAGE2-M1-report.md` | this report |

## The Nix-style assembly

The nix *products* are copied from session A's tree
(`asterinas-riscv-nixos/target/nixos/m9/rootfs`) — no recompilation, no in-guest
nix. Session A already cross-built them: `hello`/`nixos-info`/`fortune`/
`heartbeat` (musl-PIE) and the real Alpine packages `curl` 8.21.0 + `jq` 1.8.2.
We arrange them into the exact layout `nix profile install` leaves behind:

```
/nix/store/
  m9-core-1.0/bin/{hello,nixos-info,fortune,heartbeat}   # generation 1 (core.nix)
  m9-real-1.0/bin/{curl,jq}                              # generation 2 (real.nix)
  m9-profile-1.0/bin/* -> ../../m9-{core,real}-1.0/bin/* # the "user environment"
/nix/var/nix/profiles/
  default         -> default-1-link
  default-1-link  -> /nix/store/m9-profile-1.0
```

`default` is the *active* profile; every bin under it is a symlink into a store
path. This is the same indirection real NixOS uses (`/nix/var/nix/profiles/system`
→ a closure). **The store-path names are human-readable placeholders, not
content hashes** — the riscv64 nix can't run on the host, and in-guest
realisation is M9's slow ~60 s path, so the profile is *synthesized* at assembly
time from session A's already-built binaries. Content addressing is irrelevant
to this milestone (which is about *referencing* the profile, not hashing it);
the report is explicit rather than pretending the hashes are real.

### musl runtime + closure

The nix products are musl-PIE, but the desktop rootfs is glibc. So the build
adds `/lib/ld-musl-riscv64.so.1` (musl bundles libc into the loader) and the
exact dynamic closure of curl+jq into `/usr/lib` (libcurl, libssl, libcrypto,
libz, libnghttp2, libbrotli, libcares, libidn2, libunistring, libpsl, libzstd,
libjq, libonig) — ~10 MB, not the whole ~50 MB musl `/usr/lib`.

## The activation script (`/etc/activate`)

A minimal `switch-to-configuration` (idempotent, busybox-ash). It does the four
things that make "nix-profile software" reachable from a systemd environment:

1. `/run/current-system` → the active profile (the "current system" symlink).
2. `/run/current-system/sw/bin` → a symlink farm of every profile bin (the
   canonical NixOS `sw` path).
3. `/usr/local/bin/<name>` → profile bin (the task's "链进 /usr/local/bin"
   alternative to exporting PATH).
4. `/etc/profile` (login shells) **and** `/etc/environment` +
   `/etc/environment.d/10-nix.conf` (systemd's default service environment).

All three PATH carriers point at
`/nix/var/nix/profiles/default/bin:/run/current-system/sw/bin:/usr/local/bin:/usr/bin:/bin`.

The `nix-smoke.service` sets `Environment=PATH=…` explicitly so the bare-name
resolution doesn't hinge on whether PID 1 auto-applies `/etc/environment`/
`environment.d` on this kernel (that auto-application is tracked as a follow-up
below); the generated files are still delivered as the systemd-environment
artifact.

## Smoke result

`boot_systemd_nixos.py` drives the same U-Boot `booti` + bochs-framebuffer chain
as the desktop driver and greps the serial transcript for both the desktop and
nix milestones. Result (all OK):

```
=== NIXOS-STAGE2-M1 result ===
  graphical-target: OK      nix-activation: OK
  xorg-started:     OK      nix-hello:      OK
  xorg-input-devices: OK    nix-nixos-info: OK
  matchbox/xpanel/pcmanfm/xterm-started: OK
  nix-jq:           OK      nix-curl:       OK
  collection-ended: desktop-up
```

Representative serial output (ANSI-stripped):

```
__NIX_ACTIVATION_OK__ profile=/nix/var/nix/profiles/default
  sw=/run/current-system/sw
  /usr/local/bin: linked from profile bin
  generated: /etc/profile /etc/environment /etc/environment.d/10-nix.conf
___NIX_SMOKE_BEGIN___
PATH=/nix/var/nix/profiles/default/bin:/run/current-system/sw/bin:/usr/local/bin:/usr/bin:/bin
___NIX_RUN_hello___
Hello, world! (from a Nix-installed binary on Asterinas RISC-V)
___NIX_RUN_nixos-info___
  hostname : asterinas-riscv   kernel : 5.13.0   arch : riscv64
  nix      : 3 store paths, 1 profile generations
___NIX_RUN_fortune___
No systemd here — just busybox, nix, and a dream.
___NIX_RUN_jq___
jq-1.8.2
___NIX_RUN_curl___
curl 8.21.0 (riscv64-alpine-linux-musl) libcurl/8.21.0 OpenSSL/3.5.7 zlib/1.3.2
            brotli/1.2.0 zstd/1.5.7 c-ares/1.34.8 libidn2/2.3.8 libpsl/0.21.5
            nghttp2/1.70.0
___NIX_SMOKE_END___
```

`nixos-info`'s "3 store paths, 1 profile generation" is the profile *itself*
observing its own layout — the synthesized store is internally consistent. The
framebuffer screendump is captured as before (the desktop still renders under
systemd; the nix units run *before* `graphical.target` and don't disturb it).

## Three bugs found and fixed (all user-space, none kernel)

1. **systemd resolves `/bin/sh` → busybox and passes the resolved basename as
   `argv[0]`.** The desktop milestone never noticed because its units exec
   binaries by absolute path. When `nix-activation.service` first used
   `ExecStart=/bin/sh /etc/activate`, busybox saw `argv[0]="busybox"` and treated
   `/etc/activate` as an *applet name* → `status=127`. Fix: invoke the applet
   explicitly, `ExecStart=/bin/busybox sh /etc/activate`.

2. **The desktop rootfs busybox has only the `sh` applet compiled in.** The
   desktop build symlinks `mkdir`/`ln`/`cat`/`ls`/… to a busybox that reports
   `mkdir: applet not found` for every one of them. `nixos/activate` shells out
   to those, so it failed until we swapped in session A's **full musl busybox**
   (proven by M9's getty/login/rc) and re-linked the applet set. This also makes
   `/bin/sh` a genuinely usable shell for the first time in this rootfs.

3. **Self-referential symlink on `libssl.so.3` / `libcrypto.so.3`.** These two
   musl libs ship *unversioned* (no `.so.3.x` real file). The first closure-copy
   loop unconditionally ran `ln -sfn libssl.so.3 libssl.so.3`, producing a
   self-symlink; musl then reported `Error loading shared library libssl.so.3:
   Symbolic link loop`, killing only `curl` (jq/hello were unaffected). Fix:
   only re-create a symlink when the source is actually a symlink; copy regular
   files verbatim.

## Gap list (inherited from the desktop milestone — none block this one)

| Symptom | Root cause | Owner |
|---|---|---|
| `memory.max` I/O error once per service | cgroup-v2 `memory.max` read-only in this tree | kernel (session A) |
| `Failed to start device monitor: Protocol not available` | AF_NETLINK unimplemented | kernel (session A) |
| `Unimplemented syscall 258/264/293` (`riscv_hwprobe`/…/`rseq`) | glibc/musl startup probes | kernel (future) |
| `FBIOBLANK: Invalid argument` | fbdev blanking ioctl unsupported | kernel (future) |
| `StandardOutput=console` unparsable | not a valid value; use `tty`/`journal+console` | fixed in-tree |

## Reproduce

```bash
tools/riscv/systemd/gate_nixos.sh            # build initramfs + repack disk + boot + report
# serial transcript: target/systemd-desktop/serial-nixos.log
# screenshot:        /tmp/asterinas-sd-nixos.ppm
```

Preconditions: systemd 257.5 cross-built (`SYSTEMD-M2`), the desktop payload in
`target/riscv-cross/usr` (xorg/GTK milestones), a Sv39 kernel, and session A's
nix products at `../asterinas-riscv-nixos/target/nixos/m9/rootfs` (override with
`NIXOS_REPO=`).

## Next steps

1. **systemd default-environment auto-application.** The generated
   `/etc/environment` + `/etc/environment.d/10-nix.conf` are correct artifacts;
   whether PID 1 imports them (so *all* services get the nix PATH without an
   explicit `Environment=`) is unverified and likely depends on the generator
   path layout from the `meson install --prefix=/usr` cleanup. Worth confirming
   once that cleanup lands.
2. **A getty on the serial console** (M3 has the agetty recipe) so the nix
   environment is also interactively reachable, not just via units.
3. **Real content-hashed store paths** — rebuild the store with the riscv64 nix
   running in-guest once (a one-time `nix profile install`), or compute nix
   store hashes at assembly, so `/nix/store` is bit-for-bit nix-consistent.
4. **A nix-managed daemon as a unit** (M9's `heartbeat`), wiring a nix-installed
   service — not just binaries — into `graphical.target`.
