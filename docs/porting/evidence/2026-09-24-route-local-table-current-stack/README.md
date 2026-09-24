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
The change has not been installed on the physical board. The pinned
native LMBench `make results` ALL suite passed 109/109 on an ancestor of this
network stack; it was not repeated for this netlink-only change.
