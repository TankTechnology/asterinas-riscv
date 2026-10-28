# GTK-INTERACTIVE-M1 — the interactive GTK desktop actually opens

Date: 2026-08-20
Status: **MILESTONE ACHIEVED** — `boot_gtk_interactive.py` reliably pops a
QEMU GTK window on the host, the guest boots to `graphical.target`, and the
Xorg desktop (matchbox + xpanel + pcmanfm + xterm + netsurf) renders and
accepts keyboard input end-to-end (host keypress → virtio-keyboard → evdev →
Xorg → xterm). Four independent bugs along the host→guest chain were found
and fixed; no kernel changes.

## Symptoms being chased

1. The QEMU window never appeared, or appeared and stayed black.
2. When the guest did boot, the serial log showed `xorg.service` exiting with
   `status=127` and restart-looping; the second Xorg instance hung on VT1.

## Root causes & fixes

### 1. Host: QEMU GTK window silently never appears (XAUTHORITY in /tmp)

The desktop session exports `XAUTHORITY=/tmp/xauth_XXXXXX`. `/tmp` on this
host is cleaned periodically, so the cookie file disappears and QEMU's GTK
backend then fails to connect to the X server — with no obvious error at the
boot-script level. Fix on the host side (not in the repo): pin the cookie
outside /tmp and launch with it explicitly:

```bash
cp "$XAUTHORITY" ~/.xauth-qemu
DISPLAY=:0 XAUTHORITY=$HOME/.xauth-qemu \
  python3 tools/riscv/systemd/boot_gtk_interactive.py
```

### 2. Guest: Xorg exit 127 = `undefined symbol: udev_new` in evdev_drv.so

The unit ran Xorg with `StandardError=null`, hiding the fatal message. After
switching `xorg.service` to `StandardOutput=tty`/`StandardError=tty`, the
serial console showed:

```
/usr/bin/Xorg: symbol lookup error: /usr/lib/xorg/modules/input/evdev_drv.so: undefined symbol: udev_new
```

`evdev_drv.so` references `udev_*` symbols but was linked **without** a
`DT_NEEDED` for libudev (`readelf -d` shows only `libc.so.6`), so the dynamic
linker kills the process at first lazy binding — exit code 127, right after
keyboard/XKB init, before the pointer device. The framebuffer never gets
painted.

Fix (all three places kept in sync):

- `build_systemd_desktop.sh` step 8b: install `libudev.so.1.7.10` (already
  built with systemd) into `/usr/lib` plus the `libudev.so.1` SONAME symlink;
- `units/xorg.service`: `Environment=LD_PRELOAD=/usr/lib/libudev.so.1` (the
  module has no DT_NEEDED, so the library must be force-loaded);
- `units/xorg.service`: `StandardOutput=tty` / `StandardError=tty` so Xorg's
  stderr is visible on the serial console from now on.

### 3. Guest: `sh: ls: not found` in xterm — two stacked causes

- systemd's compiled-in default service `PATH`
  (`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin`) does not contain
  `/bin`, and this rootfs is unmerged-usr with the busybox applets in `/bin`.
  Fixed by `/etc/systemd/system.conf`
  (`[Manager] DefaultEnvironment=PATH=...:/sbin:/bin`), generated in
  `build_systemd_desktop.sh` step 6b.
- After PATH was fixed, `ls` became `ls: applet not found`: the shipped
  busybox was built `allnoconfig + ash` (shell only). Rebuilt
  `target/riscv-cross/src/busybox-1.36.1` with the applet set the build
  script already symlinks (ls cat echo mount umount mkdir rm ln mknod ps
  mountpoint head tail grep find test true false sleep kill sync df free
  stty) and installed it to `target/riscv-cross/usr/bin/busybox` so future
  rootfs builds pick it up.

### 4. Kernel: flaky silent hang right after the boot banner — root cause found & fixed

Roughly half to three quarters of all boots hung between the logo and the
init process, with no output even at `loglevel=warn`. Bisected with
`early_println` markers (12-attempt boot loops, per-attempt serial logs in
`~/Program/asterinas-desktop-tools/hang-logs/`):

1. The hang is in aster-block's Process-stage component: `partition::parse()`
   on the boot disk waits forever for the virtio-blk read to complete.
2. The request is submitted and kicked (`should_notify=true`; forcing the
   kick does not help) but the completion IRQ never arrives (`irq_count=0` in
   hung boots, `2` in good boots).
3. QEMU monitor register dumps during a hang show: the device's
   `InterruptStatus=1` (QEMU *did* complete the request and is asserting the
   line), the PLIC has the source pending and enabled on one context, and the
   claim register offers it — yet no hart ever claims it.
4. Per-CPU `info registers` during a hang: only **one** hart is running the
   kernel (a *random* one each boot); the other three are still parked
   pre-kernel in OpenSBI.
5. Root cause: QEMU's riscv virt machine picks a **random physical boot
   hart** each reset, while the boot used a **1-CPU `qemu-virt.dtb`** with
   `-smp 4`. The kernel therefore maps/enables PLIC sources only on hart 0's
   context; whenever the boot hart isn't hart 0, every external IRQ is
   enabled on a parked hart and never delivered. Boot hart == 0 is exactly
   the "lucky" case that booted.

Fix: `boot_gtk_interactive.py` now dumps a DTB matching its actual
`-cpu`/`-smp` at every launch (`gen_smp4_dtb()`, via
`qemu-system-riscv64 -machine virt,dumpdtb=...`). With the 4-hart DTB the
PLIC mapping covers all harts and the boot succeeds regardless of the
chosen boot hart (verified 6/6 real boots incl. harts 0/2/3).

Deeper issues left for upstream: the kernel assumes `CpuId(0)` regardless of
the physical boot hart, and the pre-generated
`target/qemu-uboot/current/qemu-virt.dtb` is a stale 1-CPU DTB that every
`-smp 4` boot flow should stop using.

### 5. Guest: Backspace echoes `^H` instead of erasing

Two stacked causes:

- The running kernel image (`target/osdk/aster-kernel-osdk-bin.Image`) was
  built ~1h *before* the KEYBOARD-M1 commit (`0ec23b009`, ECHOE/ECHOK/ECHOKE/
  VWERASE echo) landed — the deployed guest never had the echo fix. Rebuild:

  ```bash
  # needs the vDSO blobs the Dockerfile normally provides:
  git clone https://github.com/asterinas/linux_vdso.git ~/Program/linux_vdso
  (cd ~/Program/linux_vdso && git checkout 7489835)
  VDSO_LIBRARY_DIR=~/Program/linux_vdso \
    make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode
  ```

- The `XTerm` app-defaults file shipped in the rootfs turned out to be the
  **stock upstream file** — it contains no `backarrowKey` setting, so xterm
  kept sending `^H` (0x08) while the kernel's VERASE is `^?` (0x7f). Fixed by
  forcing the resource on the command line in `xterm.service`
  (`-xrm "XTerm*backarrowKey: false"`) and appending `*backarrowKey: false`
  to the app-defaults file itself.

### 7. NetSurf: link clicks "do nothing"

Not an input bug — pcmanfm clicks worked and the serial log showed the link
click triggering `fetch_curl_setup: url 'https://www.iana.org/'`, which died
with cURL code 6 (couldn't resolve host) because the GTK boot script never
attached a NIC. Fixed by adding QEMU slirp to `boot_gtk_interactive.py`:

```
-netdev user,id=net0 -device virtio-net-device,netdev=net0
```

(the kernel's eth0 is hardcoded 10.0.2.15/24, `/etc/resolv.conf` points at
slirp's 10.0.2.3 — both already in place). Verified: clicking the IANA link
loads and renders **https://www.iana.org/** over HTTPS.

### 6. Kernel: key-release loss → autorepeat storms (virtio-input SYN_REPORT)

Real and synthetic keyboards alike produced runaway key repeats (a single
press of `d` → `pwdddd…`; `Return` → a staircase of empty prompts). Root
cause in `kernel/comps/virtio/src/device/input/device.rs::handle_event`: on
`SYN_REPORT` the handler returned `false`, which **stops draining the event
queue for that IRQ**. Any events batched behind the SYN (e.g. a key release)
then sit in the virtqueue until the *next* IRQ — which only arrives with the
next physical input, or never. Xorg, never seeing the release, autorepeats
the key forever. The one-line fix is to keep draining (`return true`), which
matches what Linux's `virtinput` ISR does (drain all pending events, then
resupply buffers).

`xterm.service` also gained `ExecStartPre=/bin/sleep 5` so the terminal maps
last and matchbox focuses it reliably — netsurf otherwise steals keyboard
focus on startup, which makes the terminal appear "broken" (keystrokes land
in the browser URL bar).

## Verification

- QEMU GTK window owned by the desktop QEMU instance appears every launch
  (`xdotool search --name QEMU` → window pid == qemu pid).
- Serial log: Xorg reaches `Adding extended input device "pointer"`, no
  `symbol lookup error`, no `xorg.service` restart; netsurf renders its home
  page.
- Screendump of the GTK window shows xpanel + pcmanfm + xterm + netsurf.
- PATH fix verified in-guest: typing `ls` into xterm (via `xdotool`, host →
  virtio-keyboard → evdev → Xorg — so keyboard input is verified end-to-end
  too) changed the error from `sh: ls: not found` to `ls: applet not found`,
  i.e. the shell now finds `/bin/ls`.
- Rebuilt busybox verified on the host under qemu-user (`busybox ls` lists
  directories) and confirmed running in the guest via the xterm banner build
  timestamp.
- Backspace in xterm: verified end-to-end via QEMU monitor `sendkey`
  (deterministic press+release) with pixel evidence in
  `~/Program/asterinas-desktop-tools/evidence/`: `hello` typed clean → 5×Backspace erased all five
  characters (`# ` prompt empty, no `^H`) → `ls` + Enter listed `/`.
  Requires the §5 (backarrowKey) + §6 (SYN_REPORT drain) fixes together.
- Working initramfs + kernel + DTB backed up outside the volatile `target/`
  tree at `~/Program/backups/asterinas-desktop-20260820/`.

## Known remaining issues

- The §4 early-boot race (worked around by retrying).
- On Xorg restart, the next instance can hang on VT1 (masked now that the
  first instance no longer crashes; would resurface if Xorg ever dies).
- matchbox focus quirk: synthetic `xdotool` clicks move the pointer but do
  not always retarget keyboard focus between guest windows (keystrokes kept
  landing in netsurf's URL bar in one session; a previous session accepted
  them in xterm). Real-mouse focus switching was not investigated.
- No networking in the demo guest (netsurf's favicon fetch fails DNS — by
  design).

## Fixed after the first revision of this report

- The synthetic-input repeat storm turned out to be a real guest bug
  (§6, virtio-input SYN_REPORT early-return), not a test artifact — fixed in
  the kernel and verified with monitor `sendkey`.
- The matchbox "focus quirk" is just netsurf grabbing keyboard focus on
  startup; worked around by starting xterm last (`ExecStartPre=/bin/sleep 5`).
- If Backspace still shows `^H` in xterm on the fixed stack, the remaining
  suspect would be the pty's termios VERASE — check with the `stty` applet.
- No networking in the demo guest (netsurf's favicon fetch fails DNS — by
  design).
