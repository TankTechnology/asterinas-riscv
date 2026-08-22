# XFCE-M3 — Xfce desktop running in the riscv64 guest

Date: 2026-08-22
Status: **MILESTONE ACHIEVED** — xfce4-session brings up xfwm4 + xfce4-panel +
xfdesktop + xfsettingsd on the Asterinas riscv64 guest over the M1 D-Bus
system bus, replacing matchbox. Pixel-verified via headless QEMU screendumps
(shots/). No kernel changes.

## 1. What was built

All cross-compiled into `target/riscv-cross/usr` on top of the M2 matrix
(`build_xfce_apps.sh`, idempotent):

| package | version | build | notes |
|---|---|---|---|
| libXinerama / libXpresent | 1.1.5 / 1.0.1 | autotools | xfwm4 deps (Xinerama is non-optional in 4.20) |
| libyaml | 0.2.5 | autotools | xfdesktop settings are YAML |
| libdisplay-info | 0.2.0 | meson | hard dep of libxfce4windowing's X11 backend |
| libxfce4windowing | 4.20.0 | meson | X11-only (wayland disabled); wraps libwnck |
| xfwm4 | 4.20.0 | autotools | window manager |
| xfce4-panel | 4.20.0 | autotools | needs libxfce4windowing(ui); dbusmenu/gtk-layer-shell off |
| xfdesktop | 4.20.0 | autotools | libnotify off |
| iceauth | 1.0.10 | autotools | xfce4-session configure hard-requires it (bakes ICEAUTH_CMD for runtime) |
| xfce4-session | 4.20.0 | autotools | `--with-xsession-prefix=$PREFIX` else `make install` writes to the host's /usr/share/xsessions |
| xfce4-settings | 4.20.0 | autotools | xfsettingsd; libxklavier/colord/upower/libnotify off, XRandR/XCursor on |
| adwaita-icon-theme | 47.0 | meson | icons **and** the Adwaita Xcursor theme |
| hicolor-icon-theme | 0.18 | meson | hicolor/index.theme — the mandatory fallback theme (gtk warned it was missing) |

## 2. Session architecture (guest)

`graphical.target` → `xorg.service` + `xfce-session.service` (+ `xterm`,
`gtk3-hello` probes, `xfce-debug` dump).

- `xfce-session-start` (usr/bin) wraps the session in **dbus-run-session** —
  M1 proved only the system bus; xfconfd and the at-spi2 helpers live on the
  *session* bus. Verified in the serial log: dbus-daemon activates
  `org.xfce.Xfconf` (session bus) on behalf of xfce4-session.
- Sets XDG_RUNTIME_DIR=/run/xfce-root (0700), XDG config/data dirs,
  NO_AT_BRIDGE=1 (no a11y stack; otherwise every GTK app pays a failed
  at-spi2 bus-launch retry — its config bakes the host prefix).
- xfce4-session runs its failsafe session from xfconf system defaults:
  xfwm4, xfsettingsd, xfce4-panel, xfdesktop (Thunar entry dropped — not
  built).

## 3. Pixel verification (shots/)

Headless QEMU (`boot_xfce_desktop.py`: own boot disk + monitor socket + VNC
:9 under /tmp/xfce-m3/; nothing touches /tmp/vnc-demo or other sessions'
QEMU). Screendumps via the QEMU monitor, timed off the "panel realized" log
marker (userspace startup drifts a lot between runs, so settling off
graphical.target was unreliable).

| file | content |
|---|---|
| `shots/xfce-desktop-full.png` | xfce4-panel (Applications menu, window list, clock, user menu) + pager panel + **xfwm4-decorated xterm and gtk3-hello** (title bar with icon, shade/min/max/close buttons) |
| `shots/gtk3-probe.png` | gtk3-hello rendering on bare X (no WM) — the glib/GTK3 gate probe |
| `shots/serial-excerpt.log` | session bring-up excerpt (ANSI-stripped) |

Decorations proof: xfwm4 looks up per-window icons for the title bar
(serial: `(xfwm4): Could not find the icon 'gtk3-hello' ...`), and the
screendump shows framed windows.

## 4. The glib stability gate — PASS

GTK-M1 recorded glib-2.74-stack signal-11 crashes (openbox, lxpanel) with
`Unimplemented syscall 293` (rseq) noise. For M3 the whole glib 2.80 + GTK3
3.24 dynamic stack (xfwm4, panel, xfdesktop, xfsettingsd, xfconfd,
gtk3-hello) ran through multiple full boots with:

- **zero segfaults** and zero `Unimplemented syscall` lines in the serial
  log;
- gtk3-hello rendering correctly on bare X;
- panel/window-list/pager all functional.

This **depends on the uncommitted rseq/riscv_hwprobe work in the worktree**
(`kernel/src/syscall/rseq.rs`, `riscv_hwprobe.rs`, another session): the
current kernel image already contains it — the syscalls now return ENOSYS
cleanly instead of tripping the kernel's "Unimplemented syscall" path, and
glibc falls back gracefully. Not committed here (belongs to that session).

## 5. Bugs found and fixed while bringing the desktop up

| symptom | root cause | fix |
|---|---|---|
| guest ignored the Xfce unit set | base backup initramfs predates M1 **and** its `default.target` is a regular *file* (old graphical.target copy), not a symlink — overriding graphical.target was inert | packer re-applies M1 D-Bus payload + units from the repo and mirrors graphical.target into default.target |
| xfwm4 stalled right after its XRes probe; nothing managed | compositor init on a GLX/Present-less fbdev Xorg | xfconf system default `use_compositing=false` (xfconf-defaults/xfwm4.xml) |
| no window decorations | xfwm4 themes live in share/themes (was not packed) **and** are XPM — gdk-pixbuf had only the png builtin loader | pack `share/themes`; gdk-pixbuf `builtin_loaders=png,xpm` |
| Gdk charset conversion warnings everywhere | guest glibc runtime shipped without gconv modules | pack `/usr/lib/gconv` from the cross sysroot |
| xfsettingsd autostart file not found | libxfce4ui bakes `$PREFIX/etc/xdg/autostart` | `/usr/etc/xdg -> /etc/xdg` symlink (resolves through the M1 host-prefix bridge) |
| screen blanked mid-verification | Xorg default DPMS/screensaver | ServerFlags BlankTime/… =0 in the staged xorg.conf |
| black backdrop | xfdesktop had no channel defaults | seeded xfdesktop.xml (solid #2F6B9A) for the likely RandR monitor names |

## 6. Kernel gap list (all cosmetic — nothing blocked the desktop)

New/confirmed noise, all with working userspace fallbacks, reported for the
kernel track: `sendmmsg MSG_NOSIGNAL` unsupported (glib/dbus fallback),
`waitid(WEXITED)` warning, "only socket-level options are supported",
`POSIX_FADV_WILLNEED` ignored, XRes extension absent (xfwm4 warns, works).
Carried over: AF_NETLINK missing (no udev hotplug), no journald socket in
this rootfs (unit stdout is dropped — the session wrapper logs to ttyS0
directly). **No crash-level gaps; nothing needed an in-line kernel fix.**

## 7. Known deltas vs the x86 NixOS Xfce demo (M4 candidates)

1. Panel menu icon (`org.xfce.panel.applicationsmenu`) is SVG → missing
   without librsvg (icons show as placeholders). Build librsvg or ship
   adwaita-icon-theme-legacy.
2. No backdrop image — solid color only (xfdesktop ships no backdrops;
   distros package them separately).
3. Locale is C (glibc locales not installed): "Locale not supported"
   warnings.
4. xfsettingsd respawned once per boot (session re-reads autostart) —
   cosmetic, settles.
5. Thunar absent (failsafe entry removed); pm-is-supported absent
   (xfce4-session logout dialog greys power actions).
6. Boot-to-desktop is ~4-6 min in TCG emulation (Xorg alone ~90 s).

## 8. Reproduce

```bash
bash tools/riscv/xfce/build_xfce_deps.sh    # deps (M2 matrix)
bash tools/riscv/xfce/build_xfce_libs.sh    # six core libs (M2)
bash tools/riscv/xfce/build_xfce_apps.sh    # desktop components (M3)
bash tools/riscv/xfce/pack_xfce_initramfs.sh
python3 tools/riscv/xfce/boot_xfce_desktop.py --collect-timeout 700 --settle 240
```
