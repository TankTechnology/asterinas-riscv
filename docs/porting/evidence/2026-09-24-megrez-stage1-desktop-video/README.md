# Megrez desktop readiness and bounded local video, 2026-09-24

The normal Debian desktop boot did not start the generated
`asterinas-desktop-ready.service`: the Stage1 code linked it only into
`asterinas-debug-console.target.wants`, while this boot follows the normal
target graph and starts `getty.target`. This change links the readiness unit
into `getty.target.wants` for a normal debug-console desktop boot. The isolated
debug-console boot retains its existing target link.

The first current-kernel canary still used the installed older
`/stage1-d62ab8325e03.cpio`. Firefox became visible, but the watchdog read
`1`. A root-console check then found both
`/run/systemd/system/asterinas-desktop-ready.service` and
`/run/asterinas-tools/physical-graphics-control` absent; explicitly starting
the unit returned `Unit ... not found`. This was an artifact mismatch as well
as a target-link defect. The board was software-rebooted to a fresh U-Boot
epoch and RockOS SSH recovered with a new boot ID. See
[`old-stage1-start.json`](old-stage1-start.json) and
[`old-stage1-recovery.json`](old-stage1-recovery.json).

The tested canary pairs the RISC-V Sv39 release kernel
`/boot/asterinas-e106f84051f0.booti` (SHA-256
`e106f84051f053929131f2cf21c1eae23c4c3383c88b52f20b11a833b63d1089`)
with `/boot/stage1-cfec77d41d68.cpio` (SHA-256
`cfec77d41d68dfcb67228dfa6791d9cea25227c427ee9943ba5f44d6bd269cd1`,
1,104,384 bytes). The latter was built twice with the pinned
`asterinas/asterinas:0.18.0-20260702-riscv-rootfs` image, yielding identical
hashes; a build from this branch yielded that hash again. The separate menu
`/boot/extlinux/asterinas-current-stage1-canary-20260924.conf` has SHA-256
`304ecf835707fd2e976e29c551db31837094750b66a1d117d32a6783b2f5c42d`.
Only its Desktop entry uses the new Stage1; the default remains RockOS.

The host selected that menu over
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0` with
`sysboot mmc 1:1 any 0x88200000 /extlinux/asterinas-current-stage1-canary-20260924.conf`,
then choice `4`. The Desktop entry requests `--root-init=systemd
--debug-console=root --volatile-home` and a 420-second software recovery
timer. Fresh nonce-framed commands proved UID 0, PID 1 `systemd`, root
`/dev/mmcblk0p2:ext2`, and boot ID
`e0d83ada-86ca-4c85-95de-6a14735a5f07`. The serial connection was closed
and reopened, and the same boot ID and root authority were proved again.
Firefox had a visible window; `systemctl show` reported the readiness unit
`active/exited`, and the watchdog read back `0`. The service was not started
manually in this run. See [`desktop.json`](desktop.json) and
`desktop-serial.log` in [`raw-logs.tar.gz`](raw-logs.tar.gz).

For a narrow media check, the repository-owned HTML and 646-byte silent
VP8/WebM fixture were copied into the guest's temporary filesystem over the
root serial console, with guest SHA-256 readback. The local Firefox Marionette
gate verified JavaScript, `canplay`, and `ended`, including a positive media
time at the duration. A second version of the HTML delayed its completion
marker until eight one-second playback loops finished. That gate returned
zero in 11.439 host seconds. Firefox remained PID 252 and active, the boot ID
was unchanged, and the watchdog remained `0`; its RSS changed from 298,912
to 299,456 KiB across that brief test. See
[`one-second-video.json`](one-second-video.json),
[`eight-loop-video.json`](eight-loop-video.json), and
`eight-loop-serial.log` in [`raw-logs.tar.gz`](raw-logs.tar.gz). The gate needed a
test-local adapter because this Firefox 143 returns `{"value": null}` for a
successful Marionette `Navigate`, while the browser-M5 gate expects `null`.
The browser-web profile also does not satisfy browser-M5's loopback-only
namespace assertion, so that assertion was not applied. These checks do not
prove displayed video frames or Bilibili playback.

The simple host-served HTTP page was not reachable from this browser
configuration: a Python URL open from Firefox's network namespace returned
`EADDRNOTAVAIL`. Separately, `ip -o -4 addr show` printed `lo` and then did
not finish within the 20-second serial query; it was interrupted, after which
root control was re-proved. See `network-http-error.log` and
`network-ip-timeout.log` in [`raw-logs.tar.gz`](raw-logs.tar.gz). This network behavior
remains open and is separate from the passing local media check.

At handoff, the board remained in this Asterinas boot. A final fresh serial
connection proved UID 0 and the same boot ID; both readiness and Firefox
services were `active`, and the watchdog still read `0`. RockOS remains the
default U-Boot menu entry. The new Stage1 has not yet been checked by a
separate persistent reboot cycle. See [`handoff.json`](handoff.json). No
credentials are included in this evidence.

Host verification:

```text
python3 -m unittest tools.riscv.tests.test_debian_rootfs.DebianStage1Tests
Ran 41 tests in 5.728s — OK

tools/riscv/debian/rootfs/build_stage1.sh target/physical-20260924/initramfs.cpio
SHA-256 cfec77d41d68dfcb67228dfa6791d9cea25227c427ee9943ba5f44d6bd269cd1
```
