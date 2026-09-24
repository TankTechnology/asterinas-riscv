# IPv4 destination route lookup on the current RISC-V network stack

This branch follows the route-dump canary in PR #164. That kernel made
`ip -4 route show` work, but `ip -4 route get 10.0.2.2` still returned
`EOPNOTSUPP`. Linux `iproute2` sends this as a non-dump `RTM_GETROUTE` request
with a `/32` destination attribute. The change replies with one
`RTM_NEWROUTE` message, using the netlink socket's network namespace and the
currently configured IPv4 interface, connected prefix, and default gateway.

The focused C regression checks six destinations in QEMU: an on-link peer, an
off-link address, loopback, the interface's own address, its subnet broadcast,
and global broadcast. It checks route type, output interface, preferred
source, destination, gateway presence, sequence number, and the absence of a
dump flag. This runs as part of the existing `netlink_route_netns` gate; its
log validator requires the new success fact along with the two pre-existing
namespace facts.

The test was first run against the unchanged kernel. It failed because the
first lookup got an error instead of `RTM_NEWROUTE`; see the
[initial red log](initial-red-qemu.log.gz). Adding on-link and gateway lookup
made that case pass. The added local-address cases then failed on route type;
see the [local red log](local-red-qemu.log.gz). The broadcast cases likewise
failed before their route type was handled; see the
[broadcast red build log](broadcast-red-build.log.gz).

The final focused gate returned zero:

```sh
tools/docker/run_dev_container.sh --workspace /absolute/path/to/worktree -- \
  make run_kernel AUTO_TEST=netlink_route_netns TARGET_ARCH=riscv64 \
  SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
```

The [green QEMU log](green-qemu.log.gz) contains exactly one route-lookup
success fact and the two namespace facts. The new test compiled with
`gcc -std=gnu11 -Wall -Wextra -Werror`; 50 Python validator and native-LMBench
driver unit tests passed. Targeted Rust formatting and `git diff --check`
passed.

A separate Debian 13.7 QEMU boot used a production-kernel Image SHA-256
`c148dd2fd750cd93cd944c8e6c5bb40f195d8c9e6656938af2972748cacfef16`,
read back from the boot disk with the same hash. The frozen ext2 root disk was
`e53a1736827dd620da342f3c156eead08c0a7eee4d0b6856f2b570ef013c5e7c`;
U-Boot was
`738775f52dcfad98dfae4b851a6dc3118db30bbe6455c7df3573a84133db8a5d`.
The guest boot ID was `3f9730d0-8247-4e94-926c-91cd5a8fbee6` and the
debug console proved UID 0. Each command had a four-second deadline.

`ip -4 route get` returned status zero and printed:

```text
10.0.2.2 dev eth0 src 10.0.2.15
198.51.100.10 via 10.0.2.2 dev eth0 src 10.0.2.15
local 127.0.0.1 dev lo src 127.0.0.1
local 10.0.2.15 dev lo src 10.0.2.15
broadcast 10.0.2.255 dev eth0 src 10.0.2.15
broadcast 255.255.255.255 dev eth0 src 10.0.2.15
```

The existing main-table route dump and default filter still returned zero.
`ip -4 route show table local` remained unsupported and returned status 2
promptly. The [structured result](iproute2-result.json),
[request record](iproute2-request.json), and
[raw serial transcript](iproute2-serial.log.gz) retain the evidence.

The lookup currently covers destination-only IPv4 requests on the configured
default interface and locally assigned addresses. It does not add route
modification, arbitrary routing tables, source or output-interface filters,
IPv6 lookup, or a local-table dump. It has not been installed on the board.
The pinned native LMBench `make results` ALL suite passed 109/109 on the parent
network stack; it was not repeated after this route-lookup change.
