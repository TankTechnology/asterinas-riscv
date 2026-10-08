# IPv4 local route table on the current RISC-V network stack

This branch follows the IPv4 route-lookup change in PR #165. Before this
change, `ip -4 route show table local` returned `EOPNOTSUPP` while the main
route table and destination lookup worked. The route-dump handler now reports
the configured IPv4 local and broadcast routes from the netlink socket's
network namespace when `RTA_TABLE` selects table 255. The main-table dump and
single-destination lookup retain their previous behavior.

The new raw-netlink regression requests the local table with `RTM_GETROUTE` and
checks five routes in the one-NIC QEMU fixture: the loopback `/8` and host
address, loopback broadcast, `eth0`'s host address, and `eth0`'s subnet
broadcast. It verifies table, route type, prefix, scope, output interface,
preferred source, absence of a gateway, multipart replies, and `NLMSG_DONE`.
The general regression runner and the focused `netlink_route_netns` gate both
include this test. Its Linux-host oracle compiles with strict warnings and
completes a bounded dump; exact route identities are checked in Asterinas
because the host has different interfaces and addresses.

The focused QEMU gate first failed on the old handler because the request
returned an error rather than `RTM_NEWROUTE`; see the
[red log](red-qemu.log.gz). After the change, the same command returned zero:

```sh
tools/docker/run_dev_container.sh --workspace /absolute/path/to/worktree -- \
  make run_kernel AUTO_TEST=netlink_route_netns TARGET_ARCH=riscv64 \
  SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
```

The [green log](green-qemu.log.gz) includes exactly one success fact each for
the local dump, destination lookup, route-socket namespace, and uevent-port
namespace regressions. The C test compiled with `-Wall -Wextra -Werror` for
both the host and Asterinas variants. The 29 log-validator unit tests, targeted
Rust formatting, and `git diff --check` passed.

A separate Debian 13.7 QEMU boot used a production-kernel Image SHA-256
`9d2c86ec9784b691cbb1869b4a047fe8b1e38127785b11e409324de8e673925c`,
read back from the boot disk with the same hash. The frozen ext2 root disk was
`e53a1736827dd620da342f3c156eead08c0a7eee4d0b6856f2b570ef013c5e7c`;
U-Boot was
`738775f52dcfad98dfae4b851a6dc3118db30bbe6455c7df3573a84133db8a5d`.
The guest boot ID was `eed04b84-caf5-4c2d-804e-a1e36ea384f3`, with a UID-0
debug console. All route commands had four-second deadlines.

`ip -4 route show table local` returned zero and printed:

```text
local 127.0.0.0/8 dev lo proto kernel scope host src 127.0.0.1
local 127.0.0.1 dev lo proto kernel scope host src 127.0.0.1
broadcast 127.255.255.255 dev lo proto kernel scope link src 127.0.0.1
local 10.0.2.15 dev eth0 proto kernel scope host src 10.0.2.15
broadcast 10.0.2.255 dev eth0 proto kernel scope link src 10.0.2.15
```

`ip -4 route show`, its default filter, and `ip -4 route get 10.0.2.2`
also returned zero. The [structured result](iproute2-result.json),
[request record](iproute2-request.json), and
[serial transcript](iproute2-serial.log.gz) retain the commands and output.

## All-table dump follow-up

Linux `ip -4 route show` explicitly requests table 254, while
`ip -4 route show table all` sends a dump without `RTA_TABLE`. Before the
follow-up, Asterinas treated both requests as main-table-only. The added
regression requests an unfiltered IPv4 dump and requires exactly two main
routes and five local routes in the same bounded multipart response. It
failed on the old handler because no local routes appeared; see the
[all-table red log](all-red-qemu.log.gz). The same focused QEMU gate passed
after the handler appended both tables, in main-then-local order; see the
[all-table green log](all-green-qemu.log.gz). The raw C test also compiled with
strict warnings and completed on Linux.

The final production-kernel Image SHA-256 was
`3e3707b7e59f46303a914395a8dd4a85de55d0feb063fa26bb510130ce02b40e`,
read back from the new boot disk with the same hash. The frozen Debian root
disk and U-Boot retained the hashes above. Boot ID
`32758f0b-f036-4a31-981f-e3233fdf3717` and UID 0 were proved on the debug
console. The real `ip -4 route show table all` command returned zero and
printed the two main-table routes followed by the five local-table routes,
with `table local` shown on the latter. The separate main-table and local-table
queries, default-route filter, and `ip -4 route get 10.0.2.2` also returned
zero. See the [all-table result](all-iproute2-result.json),
[request record](all-iproute2-request.json), and
[serial transcript](all-iproute2-serial.log.gz).

This reports routes for the first configured IPv4 CIDR per interface. It does
not add route modification, arbitrary policy tables, or IPv6 local routes.
The pinned native LMBench `make results` ALL suite passed 109/109 on an ancestor
of this network stack; it was not repeated for this netlink-only change.

## Physical Megrez canary for local and all-table routes

The current production Image at commit `9690d59f2d09e374bea036a1a26d79dc215e5e86`
has SHA-256 `3e3707b7e59f46303a914395a8dd4a85de55d0feb063fa26bb510130ce02b40e`.
The host served that exact 6,071,408-byte file to RockOS, which checked its
hash before installation and read it back from `/boot` with the same hash.
The [independent candidate menu](board-candidate-menu.conf.gz) changed only the
Desktop kernel path relative to the previous canary; its SHA-256 is
`3c5ab5c39edb3f39308a0f0dbd730aee4a1023d071daca0ae0496f36ef4ae600`.
RockOS remained the default, and the previous Image and menu were retained.

The previous Asterinas boot software-rebooted into a fresh OpenSBI/U-Boot epoch
and RockOS boot ID `f2602d5d-3c82-4c21-8d5f-fac05acfcc43`.
After installation, a normal RockOS reboot reached another fresh U-Boot epoch.
The host selected the independent menu's Desktop entry, and U-Boot loaded
`/asterinas-route-all-3e3707b7e59f.booti`.
The new Asterinas boot ID was `ce042e30-d629-4421-82e6-342ce1e139d8`.
The [RockOS recovery](board-rockos-reboot.serial.log.gz),
[selected boot](board-candidate-boot.serial.log.gz), and
[structured result](board-result.json) retain the identities and selected files.

With a four-second bound on each command, `ip -4 route show` returned the
default and connected routes, `ip -4 route show table local` returned five
loopback/Ethernet local and broadcast routes, and
`ip -4 route show table all` returned those five plus the two main routes.
`ip -4 route get 10.100.16.1` selected `eth0` and source `10.100.19.200`;
the default-route filter also succeeded.
The same all-table query succeeded inside Firefox's network namespace.
The [framed route transcript](board-routes.serial.log.gz) contains the command
outputs and exit statuses.

Firefox then opened a minimal LAN page and completed its one-second VP8/WebM
clip without a decode error or dropped frame (5/5 frames).
See the [browser transcript](board-browser-media.serial.log.gz).
After desktop readiness became active and the reboot watchdog read `0`, two
separately reopened connections to
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`
each returned a fresh nonce-framed UID-0 response with the same boot ID,
`systemd` on `/dev/mmcblk0p2` (ext2), active desktop and browser services,
the board address, and a successful route lookup.
The [first](board-handoff-1.serial.log.gz) and
[second](board-handoff-2.serial.log.gz) handoff logs retain that proof.
The serial descriptor was closed after the checks.

## Controlled reboot and repeatability

From boot ID `ce042e30-d629-4421-82e6-342ce1e139d8`, a fresh UID-0
command requested `sync; reboot -f` with the software recovery watchdog at `0`.
The [reboot transcript](board-reboot-boot.serial.log.gz) observed another
OpenSBI/U-Boot epoch, read the same independent menu from MMC, and loaded the
same versioned Image after the host selected Desktop again.
The new boot ID was `80a2b08d-e2aa-4f6b-b3ae-9394770f8384`.

On this second boot, the bounded main, local, and all-table queries again
returned two, five, and seven routes, and destination lookup selected `eth0`
with source `10.100.19.200`.
See the [second route transcript](board-reboot-routes.serial.log.gz).
Firefox's network namespace returned the full route dump and fetched the
host-served WebM with HTTP 200 and 646 bytes; the
[network transcript](board-reboot-firefox-network.serial.log.gz) retains both
exit statuses.
Desktop readiness became active, the browser service remained active, and
the software reboot watchdog returned to `0`.
Two separately reopened serial connections again proved UID 0, the new boot
ID, `systemd` on the ext2 Debian root, the board address, and a successful
route lookup.
The [first](board-reboot-handoff-1.serial.log.gz),
[second](board-reboot-handoff-2.serial.log.gz), and
[structured reboot result](board-reboot-result.json) retain this handoff.
The serial descriptor was closed after verification.

This proves that the installed Image and independent menu survived a normal
software reboot and worked when explicitly reselected.
An unattended reboot still selects RockOS; the test did not change the
default entry.
The earlier 109/109 native LMBench result remains QEMU evidence from an
ancestor kernel, not a physical-board score or a rerun on this Image.
