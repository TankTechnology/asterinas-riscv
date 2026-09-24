# IPv4 route dumps on the current RISC-V network stack

The physical-board network-profile canary had an IPv4 address and could fetch
HTTP media, but `ip -4 route show` timed out. A bounded raw RTM_GETROUTE probe
received `NLMSG_ERROR(-EOPNOTSUPP)` without a terminating dump reply. The
existing `codex/qemu-route-dump` change adds IPv4 main-table dump messages;
this branch applies it on top of `codex/stream-send-iov-fault`, the stack used
for the 109/109 native LMBench QEMU qualification.

The integration exposed a network-namespace bug. Link and address netlink
handlers use the namespace owned by the sending socket, but the imported
route handler used the caller's current namespace. A socket opened before
`unshare(CLONE_NEWNET)` must still report routes from its original namespace.
The regression now checks main-table route visibility through that old socket
and a new socket opened after `unshare`.

## Red/green QEMU result

The targeted command was:

```sh
tools/docker/run_dev_container.sh --workspace /absolute/path/to/worktree -- \
  make run_kernel AUTO_TEST=netlink_route_netns TARGET_ARCH=riscv64 \
  SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
```

Before the namespace fix, the old socket saw a route before `unshare`, while
the new namespace correctly had no main-table route. Querying the old socket
again failed at `netlink_route_netns.c:107` because the handler read the new
current namespace. The command returned status 2. After passing the socket's
`NetNamespace` into the route handler, the same command returned zero and its
QEMU transcript passed the `netlink-route-netns` validator. The full
[red](netns-red-qemu.log.gz) and [green](netns-green-qemu.log.gz) logs are
retained. Only the namespace source changed between these two builds.

## User-visible route command

A separate QEMU boot used the rebuilt Image SHA-256
`052656e9b12ce1586a76e18c75acece62c748bb3e6c9430d764fd0d7436ff116`,
the frozen Debian ext2 input SHA-256
`e53a1736827dd620da342f3c156eead08c0a7eee4d0b6856f2b570ef013c5e7c`,
U-Boot SHA-256
`738775f52dcfad98dfae4b851a6dc3118db30bbe6455c7df3573a84133db8a5d`,
and Stage1 SHA-256
`872e38f947e18c225f2df47a645d419c04c42d536a6c5f518226dbc9d243593d`.
The guest boot ID was `529bbb45-47ba-4f74-b955-278140045cf4`, with a
UID-0 debug console. Each route command had a four-second deadline.

`ip -4 route show` returned zero and printed:

```text
default via 10.0.2.2 dev eth0
10.0.2.0/24 dev eth0 proto kernel scope link src 10.0.2.15
```

`ip -4 route show default`, `ip -6 route show`, and `ip -brief addr` also
returned zero. Unsupported `ip -4 route show table local` and
`ip -4 route get 10.0.2.2` returned status 2 promptly, rather than timing
out. The [structured result](iproute2-result.json) and
[serial transcript](iproute2-serial.log.gz) retain the commands and output.

The C `route_dump.c` regression was compiled into the RISC-V initramfs and
passed 52 checks on Linux; the namespace regression ran inside Asterinas.
Targeted Rust formatting with edition 2024 and `git diff --check` passed.
No physical-board kernel was replaced for this test.

This change reports configured IPv4 main-table connected and default routes.
It does not add route modification, other tables, single-destination lookup,
or IPv6 routes. It does not establish browser latency or video-frame
presentation. The native LMBench ALL suite was qualified on the parent stack;
it was not rerun after this route-dump change.
