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

The first host-served HTTP attempt returned `EADDRNOTAVAIL` because that
canary's Desktop entry had no physical IPv4 boot profile. A later bounded
diagnostic found `ip -o -4 addr show` completed and listed only `lo`; the
following `ip -4 route show` command was the one that timed out. The original
console traces remain in `network-http-error.log` and `network-ip-timeout.log`
inside [`raw-logs.tar.gz`](raw-logs.tar.gz); the later diagnosis corrects the
earlier interpretation of that combined serial query.

At the first handoff, the board remained in the earlier Asterinas boot. A
fresh serial connection proved UID 0 and the same boot ID; both readiness
and Firefox services were `active`, and the watchdog read `0`. See
[`handoff.json`](handoff.json). No credentials are included in this evidence.

## Network-profile desktop follow-up

The board was software-rebooted to a fresh U-Boot epoch and RockOS SSH
recovered before selecting the immutable
`/boot/extlinux/asterinas-current-network-canary-20260924.conf` menu (SHA-256
`ef513c433a58aba33379481bfd11cebcc62db5e5434ef6deeaa0472505524aec`).
Its Desktop entry uses the same kernel and corrected Stage1 hashes above,
plus `asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1` and
static neighbor entries for the gateway and test host. The default menu
entry remains RockOS; the Basic and Probe entries were not changed.

This new boot was `9548084c-0d9f-4654-81c1-f82adf69ea58`. Fresh serial
root proof and a close/reopen check found PID 1 `systemd`, root filesystem
`/dev/mmcblk0p2:ext2`, Firefox visible, readiness service `active/exited`,
and watchdog `0`. Firefox's network namespace reported
`10.100.19.200/21` on `eth0`; a Python HTTP request from that namespace
received status 200 from the host at `10.100.19.216:17894`.
See [`network-profile-recovery.json`](network-profile-recovery.json) and
[`network-desktop.json`](network-desktop.json).

The host served a simple page and the same 646-byte silent VP8/WebM fixture
over HTTP. For the passing run, both the page and media had fresh URLs; the
host server observed GETs for each from `10.100.19.200`, each with status 200.
A subsequent request from Firefox's network namespace reproduced both 200
responses; its retained [`HTTP server log`](http-server.log) records the two
board-origin requests.
The Firefox Marionette gate observed JavaScript, `canplay`, and eight `ended`
events in 11.593 seconds, with no media error. Firefox remained PID 353,
its service stayed active, the boot ID was unchanged, and the watchdog stayed
`0`. The page declares a data-URL icon so an unrelated automatic favicon
request does not violate the gate's resource allowlist. The Firefox 143
`Navigate` response still needed the test-local adapter described above.
See [`http-eight-loop.json`](http-eight-loop.json). This is a small, silent,
local HTTP video check; it does not establish visible frame presentation,
remote-site playback, or browser performance.

After the video run, two independently reopened serial connections each
proved UID 0 and the same boot ID, `active` readiness and Firefox services,
watchdog `0`, and `eth0` IPv4 configuration. See
[`network-handoff.json`](network-handoff.json). The network-profile menu has
not been checked through a separate persistence reboot. `ip -4 route show`
still times out: a bounded raw RTM_GETROUTE dump probe received
`NLMSG_ERROR(-EOPNOTSUPP)` without a terminating dump reply. Route-netlink
support remains a separate kernel gap. RockOS is still the default U-Boot
entry. The new Stage1 has not yet been checked by a separate persistence
reboot cycle.

Host verification:

```text
python3 -m unittest tools.riscv.tests.test_debian_rootfs.DebianStage1Tests
Ran 41 tests in 5.728s — OK

tools/riscv/debian/rootfs/build_stage1.sh target/physical-20260924/initramfs.cpio
SHA-256 cfec77d41d68dfcb67228dfa6791d9cea25227c427ee9943ba5f44d6bd269cd1
```
