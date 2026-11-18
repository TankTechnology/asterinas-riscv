# The Megrez desktop, after the rebase (2026-11-18)

The rebased kernel reaches a running desktop on the board: Xorg on `/dev/fb0`,
openbox, pcmanfm, lxpanel and Firefox, driven from the USB keyboard and mouse.
This page records what was missing, how it was found, and what is still open.

## What the board did before

Every boot ended the same way: the console went silent a few seconds into
userspace, `asterinas-desktop-ready.service` failed, and the safe-reboot
watchdog eventually quiesced into a `sync` that never returned, so the board
had to be power-cycled. From the outside it looked like "the board hangs", and
each of the defects below was hidden behind the previous one.

## The defects this session fixed

| # | Defect | Symptom | Fix |
| --- | --- | --- | --- |
| 1 | Upstream `28abe1d5a "Add devtmpfs"` dropped the `char::init_in_first_process()` call | `/dev` had only `console`, the memory devices, `kmsg` and `loop*`: no `tty*`, no `fb0`, no `dri`, no `input`. `asterinas-desktop-m5.service` failed with `208/STDIN`, Xorg had no framebuffer | `c02de2c3a` |
| 2 | Downstream `bd966a253` (sysfs graphics class) was lost in the rebase | Xorg's fbdev driver exits with `(EE) No devices detected.` / `no screens found` even though `/dev/fb0` answers every ioctl | `947f5386e` |
| 3 | The DWC3 hosts were never selected | No USB at all: `/dev/input` absent although the keyboard and mouse are plugged in and U-Boot sees them | `/chosen/asterinas,usb-host` in the board DTB (see below) |
| 4 | Boot HID devices were registered on the first report | The gate waits for `/dev/input/event*` before starting X, and the node only appeared when someone pressed a key; the keyboard showed up 457 s into the boot and the gate had already timed out | `e220093b6` |

`fbdevHWProbe()` in `libfbdevhw.so` is what makes defect 2 fatal: after
`open(dev, O_RDWR)` it reads `/sys/class/graphics/fbN/device/subsystem` and
returns failure when that link is missing, so a framebuffer without a class
device is invisible to Xorg however well its ioctls work.

## The board boot recipe

The kernel is loaded by U-Boot from the SD card; the DTB decides whether the
on-SoC USB controllers exist at all:

```
ext4load mmc 1:3 0x80200000 kernel-rel.img
ext4load mmc 1:1 0x88300000 stage1-daily.cpio ; setenv initrd_size ${filesize}
ext4load mmc 1:1 0x88000000 megrez-daily.dtb
fdt set /chosen asterinas,usb-host \
    /soc/usb0@50480000/dwc3@50480000 /soc/usb1@50490000/dwc3@50490000
setenv bootargs ${as_args}
booti 0x80200000 0x88300000:${initrd_size} 0x88000000
```

`fdt set` writes a NUL-separated list; both controllers must be named or only
one of the two HID devices comes up. `fdt print /chosen asterinas,usb-host`
before `booti` is a two-second check that the property really holds two paths.

The boot line that reached a desktop:

```
console=ttyS0 loglevel=info init=/init systemd.show_status=yes
systemd.log_target=console asterinas.mmc_write_partition2
asterinas.mmc_write_partition3 asterinas.reboot_after=1800
asterinas.hardware_watchdog_handoff=1 asterinas.panic_reboot=1
asterinas.net=eic7700-rj45,<dhcp address>/255.255.255.0,58.206.232.1
-- --root-init=systemd --debug-console=isolated-root
```

## Evidence

The last plain boot (saved `bootcmd`, no manual intervention) produced:

```
physical-graphics-control: stage=input-wait
physical-graphics-control: stage=input-ready
physical-graphics-control: stage=desktop-start
physical-graphics-control: stage=desktop-ready
physical-graphics-control: stage=browser-start
BROWSER_WEB_DESKTOP_STAGE=display-provider-ready  provider=fbdev
BROWSER_WEB_DESKTOP_STAGE=xorg-start
BROWSER_WEB_DESKTOP_STAGE=x-socket-ready          pid=144
BROWSER_WEB_DESKTOP_STAGE=window-manager-start    pid=283
BROWSER_WEB_DESKTOP_STAGE=desktop-start           pid=286
BROWSER_WEB_DESKTOP_STAGE=panel-start             pid=288
browser-web-firefox: ASTERINAS_FIREFOX_WEB_EXEC   pid=156
ASTERINAS_SOFTWARE_REBOOT_DISARMED
```

`ASTERINAS_SOFTWARE_REBOOT_DISARMED` is the decisive marker: the safe-reboot
helper only disarms when `desktop_handoff_ready()` sees the readiness file for
this boot id together with a disarmed watchdog.

## What is still open

1. **The console goes silent when the desktop starts.** The system stays up
   (`desktop-ready` and the disarm both arrive), but nothing is printed after
   Xorg takes the VT. Candidate: the VT console painting 1920x1080 through the
   framebuffer console (`asterinas.vt_framebuffer`, enabled by default). QEMU
   boots fine with `asterinas.vt_framebuffer=0`; the board has not been tried.
2. **`DESKTOP_BOOT_READY` / `ASTERINAS_DESKTOP_DISPLAY_READY` never appear.**
   `boot_phase` writes them through `/dev/kmsg`, which is still a stub
   (`MemFile::Kmsg` returns `EINVAL`), and `<6>` is filtered by
   `loglevel=warn` anyway. Both need the kmsg wiring, and the marker should
   also be written to `/dev/console`.
3. **The hardware watchdog is not armed on a plain boot.** The handoff fails
   with `component-mismatch value=0x0` because the block is still held in
   reset; U-Boot's arming sequence (`mw.l 0x51828444 0x1`, then TOP, kick,
   enable) fixes it, and with that in place the kernel reports
   `HANDOFF status=active period_seconds=1` and a frozen board resets itself
   within 10.7 s.
4. **The safe-reboot `sync` can hang forever.** `WaitQueue::wait_until` takes an
   uncancellable predicate, so neither `timeout` nor a signal can interrupt a
   `sync` that is waiting on the block layer, and the forced-reboot fallback
   never runs. That is why a wedged boot needed a power cycle.

## The self-recovery loop, measured

With the arming sequence persisted in the U-Boot environment (`saveenv`), a
boot no longer needs anything typed by hand:

```
9094936 bytes read in 132 ms        kernel
1105408 bytes read in  61 ms        stage1 initramfs
155097 bytes read in  55 ms         dtb
Starting kernel ...
ASTERINAS_HARDWARE_WATCHDOG_HANDOFF status=active period_seconds=1
usb: USB boot mouse registered: 30fa:0302      (2.388 s)
usb: USB boot keyboard registered: 046d:c31c   (2.396 s)
stage=input-wait -> input-ready -> desktop-start -> desktop-ready -> browser-start
```

The desktop then stalls the kernel (see the open item below), and about ten
seconds later the SoC resets itself. U-Boot comes back, autoboots the same
`bootcmd` with no operator input, and the next boot reports
`status=active` again. Two consecutive cycles were observed; one cycle takes
roughly a minute.

The U-Boot side of that recipe needs one detail that is easy to get wrong:

```
setenv bc1 'load kernel; load initramfs; setenv initrd_size ${filesize}; load dtb;'
setenv bc2 'mw.l 0x51828444 0x1; mw.l 0x50800004 0xf; mw.l 0x5080000c 0x76; mw.l 0x50800000 0x1f;'
setenv bc3 'setenv bootargs ${as_args}; booti ...'
setenv bootcmd "${bc1} ${bc2} ${bc3}"     # double quotes
```

Double quotes, not single: `run` only splits the variable's own text on `;`,
so a `bootcmd` that merely expands to the pieces passes the whole line to
`ext4load` and fails with its usage message. With double quotes the pieces are
expanded while the variable is set, and the stored text carries real
semicolons.

## Debugging the desktop in QEMU over the serial console

QEMU can reproduce the whole board flow, including the gate, if two details are
right. Both cost an evening to find:

```
qemu-system-riscv64 -machine virt -cpu rv64 -smp 2 -m 2G -display none \
  -dtb virt-fb.dtb -snapshot \
  -chardev socket,id=ser0,path=/tmp/q.sock,server=on,wait=off,logfile=/tmp/q.log \
  -serial chardev:ser0 \
  -kernel target/osdk/asterinas/asterinas-osdk-bin.Image \
  -initrd /tmp/qemu-stage1.cpio \
  -drive file=target/debian-riscv/browser-web/rootfs/debian-root.ext2,format=raw,if=none,id=rootdisk \
  -device virtio-blk-device,drive=rootdisk \
  -device virtio-keyboard-device -device virtio-tablet-device \
  -append "console=ttyS0 loglevel=warn init=/init systemd.show_status=yes \
    systemd.log_target=console \
    systemd.setenv=ASTERINAS_DESKTOP_INPUT_IDENTITY_MODE=ordered \
    -- --root-init=systemd --debug-console=isolated-root"
```

* `--debug-console=isolated-root` after the `--` is what puts a root shell on
  the serial console (`root@asterinas-debug:/#`). Without it the console is
  write-only and every command appears to do nothing.
* `-device virtio-tablet-device`, not `virtio-mouse-device`. The gate's
  identity table recognises `(6, "QEMU Virtio Tablet", "virtio/input0")`; a
  virtio mouse reports as `"QEMU Virtio Mouse"`, so the gate reports the mouse
  as missing and fails with `status=125` after its input window, even though
  `/dev/input/event1` exists.

With those two, the guest reaches the same chain as the board and the readiness
markers appear — which they never did before the `/dev/kmsg` wiring, because
`boot_phase` wrote them into a stub:

```
stage=input-wait -> input-ready -> desktop-start -> desktop-ready -> browser-start
FBDEV(0): using /dev/fb0 / Depth 24 / framebuffer bpp 32
ASTERINAS_DESKTOP_DISPLAY_READY
ASTERINAS_DESKTOP_FIREFOX_READY pid=181 user=1000 optional=1
ASTERINAS_DESKTOP_BOOT_READY firefox_pid=181
```

`journalctl -k` shows kernel records in the guest from this same change, so the
console is a usable debug channel for both userspace and the kernel.

## The board stalls; QEMU does not

Five QEMU boots with the board's boot arguments do **not** reproduce the board's
stall, and the first attempt to measure it was wrong in a way worth recording.

The board stops about a minute into a desktop boot and only the hardware
watchdog brings it back. In QEMU the guest goes quiet at `stage=input-wait`
and stays quiet — which looks identical from the log. It is not: after ninety
seconds of silence a probe sent to the serial console is echoed and executed,

```
echo QEMU_ALIVE_PROBE
QEMU_ALIVE_PROBE
root@asterinas-debug:/#
```

so the QEMU guest is alive and merely quiet, because the gate polls its input
window without printing anything and `desktop-input-identity` times out on
every attempt:

```
A_SIGKILL_PROVENANCE stage=delivery origin=pending-signal target_comm="timeout" target_pid=110
A_SIGKILL_PROVENANCE stage=delivery origin=pending-signal target_comm="python3" target_pid=111
```

Two consequences:

* **Silence in the log is not evidence of a hang.** Any harness that judges a
  boot by "the log stopped" must probe the guest afterwards; this one did not,
  and its first five verdicts were meaningless.
* **The stall is board specific.** It needs something QEMU does not model —
  the DWC3 input path, the EIC7700 display, or the board's timer behaviour —
  so it has to be diagnosed on hardware, with the kernel log now available
  through `/dev/kmsg` and the durable journal.

On the board the same window is short: the gate prints `stage=input-wait` and
about six seconds later the console and the kernel log both stop, which is why
the durable heartbeat and the persistent journal were added before the next
board session.
