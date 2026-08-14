# Asterinas RISC-V — full-system demo (DEMO-M1)

A one-command demo of the complete Asterinas RISC-V system: the kernel boots
through U-Boot, hands off to **systemd** as PID 1, which starts a **graphical
desktop** (Xorg + matchbox + xpanel + pcmanfm + xterm) *and* activates a
**nix profile** into the systemd environment — all in QEMU, headless, with a
screendump as evidence.

```
$ tools/riscv/demo-all.sh
```

That single command builds the rootfs, boots QEMU, and leaves three artifacts
under `target/demo/`:

| Artifact | What it is |
|---|---|
| `target/demo/systemd-boot.log` | ANSI-stripped systemd startup transcript |
| `target/demo/asterinas-desktop.png` | the rendered desktop (1280×1024) |
| `target/demo/asterinas-desktop.ppm` | raw QEMU screendump (source of the PNG) |

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│ QEMU -machine virt  (2 GiB, rv64 Sv39)                              │
│   bochs-display · virtio-blk · virtio-keyboard · virtio-tablet      │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ boot ROM
┌───────────────────────────────▼─────────────────────────────────────┐
│ U-Boot  (qemu-riscv64_smode_defconfig)                              │
│   · loads kernel / initramfs / DTB off the virtio disk              │
│   · injects a simple-framebuffer node (bochs @0x40000000)           │
│   · booti <kernel> <initrd>:<size> <dtb>                            │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────────┐
│ Asterinas kernel (riscv64, Sv39)                                    │
│   · parses the injected framebuffer → /dev/fb0                     │
│   · /dev/input/event* (keyboard + tablet)                           │
│   · mounts the initramfs, runs /init                               │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ exec
┌───────────────────────────────▼─────────────────────────────────────┐
│ /init  (static launcher) → exec systemd 257.5 as PID 1              │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────────┐
│ systemd  (PID 1, system mode)                                       │
│   basic.target → multi-user.target → graphical.target               │
│                                                                     │
│   graphical.target Wants:                                           │
│     nix-activation.service  ── /etc/activate (mini switch-to-config)│
│     nix-smoke.service       ── hello · nixos-info · jq · curl       │
│     xorg.service            ── Xorg (fbdev, shadow framebuffer)     │
│     matchbox-window-manager ── WM (reparents client windows)        │
│     xpanel                  ── desktop panel bar                    │
│     pcmanfm                 ── GTK2 file manager                    │
│     xterm                   ── X11 terminal                         │
└─────────────────────────────────────────────────────────────────────┘
```

The nix profile is the canonical NixOS layout — `/nix/store` +
`/nix/var/nix/profiles/default` — synthesized at assembly time from already
cross-built binaries (no in-guest nix), so `/etc/activate` only *references*
the profile (symlinks + PATH) rather than hashing it.

## Boot flow (what `demo-all.sh` does)

1. **Build the rootfs** — `build_systemd_desktop_nix.sh` layers the systemd
   base (`build_systemd_desktop.sh`) with the nix profile, then packs a raw
   newc cpio (no gzip; the kernel's inflater is unreliable above ~16 MB).
2. **Re-pack the boot disk** — writes `asterinas.booti` (kernel Image),
   `initramfs.cpio.gz` (the rootfs), and `qemu-virt.dtb` into `boot.ext4`.
3. **Boot QEMU** — `boot_systemd_nixos.py` drives the U-Boot `booti` handoff,
   greps the serial for the systemd + desktop + nix milestones, waits ~60 s
   after `graphical.target` for the desktop to finish rendering, then
   screendumps the bochs framebuffer.
4. **Post-process** — strip ANSI from the log and convert the PPM to PNG.

## One-click command

```bash
tools/riscv/demo-all.sh                          # build + boot + screenshot
tools/riscv/demo-all.sh --skip-build             # reuse the existing initramfs
tools/riscv/demo-all.sh --settle-seconds 60      # longer render before the shot
tools/riscv/demo-all.sh --collect-timeout 300    # boot collection timeout
```

Options:

| Flag | Default | Meaning |
|---|---|---|
| `--skip-build` | off | reuse the existing initramfs (skip the ~1 min rootfs build) |
| `--settle-seconds N` | `60` | extra seconds after `graphical.target` before the screendump |
| `--collect-timeout N` | `300` | serial collection deadline (seconds) |

## Prerequisites

The demo **does not rebuild the kernel** (the kernel is unchanged by this
milestone). It needs these to already exist:

- a Sv39 kernel Image at `target/osdk/aster-kernel-osdk-bin.Image`
  (`make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode`), or set
  `ASTERINAS_RISCV_BOOTI=`;
- U-Boot and a boot disk (from `tools/riscv/prepare_qemu_uboot_booti.sh prepare`);
- the systemd + desktop cross-build in `target/riscv-cross/` and the nix
  products in the sibling `asterinas-riscv-nixos` tree (`NIXOS_REPO=` to override).

Full assembly details are in
[`tools/riscv/systemd/DEMO-M1-report.md`](tools/riscv/systemd/DEMO-M1-report.md)
and the milestone reports next to it.
