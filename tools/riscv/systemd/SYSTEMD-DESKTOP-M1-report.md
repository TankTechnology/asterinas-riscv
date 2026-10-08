# SYSTEMD-DESKTOP-M1 — the desktop session becomes a systemd `graphical.target`

Date: 2026-08-14
Status: **MILESTONE ACHIEVED** — systemd 257.5 boots as PID 1 on Asterinas
riscv64, reaches `graphical.target`, and starts the desktop session (Xorg +
matchbox-window-manager + xpanel + pcmanfm + xterm) as systemd services. The
framebuffer is rendered and pixel-verified. This is the first time systemd
actually *spawns and supervises unit processes* on this kernel (the sibling
SYSTEMD-BOOT milestone only reached targets, which are pure job states with no
process).

## Objective

The sibling tree's SYSTEMD-BOOT milestone proved systemd reaches `basic.target`
on this kernel. This tree's xorg/GTK milestones proved the desktop session
(Xorg on `/dev/fb0` + a matchbox/xpanel/pcmanfm/xterm session) runs under a
hand-written static `/init` (`tools/riscv/xorg/init.c`) that forks everything
itself. This milestone replaces that hand-rolled process supervisor with
systemd: the display server and every session client become units wired into a
`graphical.target`, and systemd becomes the thing that forks, respawns, and
tears them down. No kernel changes — unit files and component assembly only.

## Deliverables (`tools/riscv/systemd/`)

| File | Purpose |
|---|---|
| `units/*.target` / `units/*.service` | the `graphical.target` unit set (targets + xorg + 4 session clients) |
| `build_systemd_desktop.sh` | assemble the systemd + desktop rootfs, pack as raw newc cpio |
| `init.c` | static `/init` launcher that `exec()`s systemd as PID 1 |
| `boot_systemd_desktop.py` | QEMU driver: U-Boot `booti` + bochs framebuffer DTB injection → collect → report |
| `gate_desktop.sh` | one-command gate: build → repack boot disk → boot → report |
| `SYSTEMD-DESKTOP-M1-report.md` | this report |

## Unit set — the `graphical.target` hierarchy

`default.target → graphical.target → multi-user.target → basic.target`, with the
desktop services pulled in by `graphical.target`:

```
graphical.target
  Requires=multi-user.target
  Wants=xorg.service matchbox-window-manager.service
        xpanel.service pcmanfm.service xterm.service
  After=multi-user.target
```

- **`xorg.service`** — `Type=simple`, `ExecStart=/usr/bin/Xorg -config
  /etc/xorg.conf -modulepath /usr/lib/xorg/modules -xkbdir /usr/share/X11/xkb
  -logfile /dev/ttyS0 -noreset`. `Restart=on-failure`, `Before=` every client,
  `Conflicts=rescue.service`. `-noreset` keeps the server from clean-exiting
  (exit 0) when a client momentarily drops, which `Restart=on-failure` would
  *not* catch.
- **client units** — each `After=xorg.service`, `Requires=xorg.service`,
  `PartOf=graphical.target`, `Type=simple`, with `Environment=DISPLAY=:0`,
  `HOME=/root`, `FONTCONFIG_FILE=/etc/fonts/fonts.conf` (the same three env
  vars `tools/riscv/xorg/init.c` set before exec'ing each client). The three
  that can exit before X is ready (`matchbox-window-manager`, `pcmanfm`,
  `xterm`) get `Restart=always` + `RestartSec=2s` +
  `StartLimitIntervalSec=0` (in `[Unit]` — systemd 257 rejects it in
  `[Service]`); `xpanel` has an internal `XOpenDisplay` retry and stays up with
  `Restart=on-failure`.

## Assembly

`build_systemd_desktop.sh` is a direct extension of the sibling tree's proven
`build_systemd_boot.sh`, with the desktop payload layered on. Key steps:

1. Static `/init` launcher (the sibling tree's `init.c`, verbatim) — mounts
   `/run` tmpfs, `exec`s `systemd`.
2. glibc 2.41 runtime (7 libs) from the proven `target/xorg-rootfs/lib`.
3. All 69 systemd ELFs + the two internal `.so` files, **stripped at assembly
   time** (`--strip-unneeded`; the build tree is unstripped with debug_info —
   45 MB → 8.5 MB). The baked-host-path bridge `…/target/riscv-cross/usr → /usr`
   is created so every `config.h` host path resolves to the guest's `/usr`.
4. Desktop: Xorg + modules + libxcvt, the 4 session clients, xkbcomp (reaches
   `/usr/bin` through the bridge), xkeyboard-config, fonts, pcmanfm/libfm data,
   terminfo `x/`.
5. Packed as **raw newc cpio (no gzip)** — the kernel's zune-inflate decoder
   hangs non-deterministically on >16 MB gzip, and this rootfs is far larger.

The first cut bundled `gtk-hello` too, which is a 14 MB static GTK2 binary and
not part of this milestone's component set — dropping it brought the raw
initramfs from 77 MB down to **64 MB**, comfortably under the ~80 MB
initrd-load ceiling (INITRD_LOAD=0x83000000, DTB_LOAD=0x88000000).

## Result — the boot

Stripped-of-ANSI serial transcript (all present, in order):

```
>>> systemd init: launching systemd (PID 1) <<<
systemd 257.5 running in system mode (-PAM -AUDIT … +SYSVINIT …)
Detected architecture riscv64.
[  OK  ] Reached target Local File Systems.
[  OK  ] Reached target Swap.
[  OK  ] Reached target System Initialization.
[  OK  ] Reached target Basic System.
[  OK  ] Reached target Multi-User System.
[  OK  ] Reached target Graphical Interface.
[  OK  ] Started Xorg display server.
[  OK  ] Started Matchbox window manager.
[  OK  ] Started PCManFM file manager.
[  OK  ] Started Xpanel (pure-X11 desktop panel).
[  OK  ] Started XTerm terminal emulator.
X.Org X Server 1.21.1.15
… FBDEV(0): using shadow framebuffer … Virtual size is 1280x1024 …
(II) XINPUT: Adding extended input device "keyboard" (type: KEYBOARD, id 6)
(II) XINPUT: Adding extended input device "pointer" (type: TOUCHSCREEN, id 7)
```

This satisfies the success criterion: **systemd reaches `graphical.target` and
`xorg.service` + the four session clients are `Started` by systemd**, and
Xorg's own log confirms it brought up the fbdev shadow framebuffer and both
input devices. There are no `Main process exited` / `Failed with result` lines —
the clients connect on the first try (Xorg's `/tmp/.X11-unix/X0` socket exists
early, so `XOpenDisplay` blocks until the server is ready rather than failing).

## Verification — the framebuffer actually rendered

`boot_systemd_desktop.py` screendumps the bochs framebuffer at the end of
collection. Pixel analysis of the 1280×1024 capture matches the desktop layout
established in the xorg/GTK milestones:

| color | share | what it is |
|---|---|---|
| `#ffffff` | 88.0% | xterm / pcmanfm window backgrounds |
| `#202028` | 3.8% | xpanel bar |
| `#dcdad5` | 2.9% | GTK2 client content (pcmanfm) |
| `#496179` | 1.5% | matchbox titlebar |
| `#697d96` / `#384961` | ~0.2% | matchbox frame edges |

The WM titlebars/frames, the panel bar, and the GTK2 content are all present,
which can only happen if matchbox-window-manager is alive and has reparented the
client windows. So the session is not merely "forked by systemd" — it is
running and rendering.

## Gap list (all inherited from the sibling boot — none block the desktop)

| Symptom | Root cause | Owner |
|---|---|---|
| `xorg.service: Failed to set 'memory.max' … Input/output error` (once per service) | cgroup-v2 `memory.max` is read-only in this tree's kernel (the sibling tree fixed it after this tree forked) | kernel (session A) |
| `Failed to start device monitor: Protocol not available` | **AF_NETLINK** unimplemented — systemd's udev device monitor can't start | kernel (session A) |
| `Unimplemented syscall number: 258/293` (`riscv_hwprobe`/`rseq`) | glibc startup probes — harmless | kernel (future) |
| `FBIOBLANK: Invalid argument` | fbdev blanking ioctl unsupported — Xorg disables blanking and continues | kernel (future) |

None of these prevent the desktop. `/dev/fb0`, `/dev/input/event*`, and
`/dev/ttyS0` are populated by the kernel's initial ramfs and survive systemd's
`mount_setup_early()` (devtmpfs isn't implemented, so the mount fails and the
ramfs `/dev` stays intact).

## Reproduce

```bash
tools/riscv/systemd/gate_desktop.sh            # build initramfs + repack disk + boot + report
# serial transcript: target/systemd-desktop/serial.log
# screenshot:        /tmp/asterinas-sd-desktop.ppm
```

Preconditions: systemd 257.5 cross-built into
`target/riscv-cross/src/systemd-257.5/build-riscv` (SYSTEMD-M2), the desktop
binaries + data in `target/riscv-cross/usr` (xorg/GTK milestones), and a
Sv39 kernel (`make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode`).

## Next steps

1. **`Restart=always` hardening under real failure** — the current boot never
   exercises the restart path (clients connect first-try). Worth a synthetic
   test (kill a client, confirm systemd respawns it) once the kernel gaps above
   are closed.
2. **A display-manager-style `xorg.service`** — split the X server from the
   session (e.g. `xdm`/`lightdm`-style) so `systemctl isolate graphical.target`
   cleanly tears down and restarts the whole session.
3. **`meson install` with `--prefix=/usr`** — drop the baked-host-path symlink
   bridge (shared with SYSTEMD-M3's "会师" note) so the rootfs no longer needs
   the `…/target/riscv-cross/usr → /usr` shim.
4. **udev/dbus in the session** — once AF_NETLINK lands, wire in
   `systemd-udevd` (M3's inventory is ready) so hotplug and the D-Bus system
   bus (for policykit/upower-style session services) come up under systemd.
