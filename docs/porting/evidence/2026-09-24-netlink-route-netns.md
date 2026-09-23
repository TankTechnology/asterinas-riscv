# Route netlink socket namespace on RISC-V QEMU

Linux binds a netlink socket to the network namespace in which it was created.
Its port lookup also includes the socket namespace.
[Linux source](https://github.com/torvalds/linux/blob/master/net/netlink/af_netlink.c)
Asterinas already recorded the namespace in `NetlinkSocket`, but route request
handlers used `current_net_ns()`. After `unshare(CLONE_NEWNET)`, a pre-existing
route socket therefore queried the new namespace instead of its own.

The focused regression creates and connects a route netlink socket before
`unshare`, then creates a second one afterwards. It uses libnl `RTM_GETLINK`
and `RTM_GETADDR` dumps to check that the new socket cannot see `eth0`, while
the inherited old socket still sees `eth0` and its IPv4 address. QEMU provides
a virtio NIC so that the initial namespace has `eth0`.

With only the regression installed, the old kernel failed at
`netlink_route_netns.c:76`: `has_link(old_sock, "eth0")` was false after
`unshare`. The fix passes the socket's saved namespace through the bound
netlink state to the route request handlers. No route handler now resolves the
calling thread's current namespace.

The RISC-V QEMU command is:

```sh
make run_kernel AUTO_TEST=netlink_route_netns TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
```

The final run passed the new namespace regression and the existing
`netlink_route` and `rtnl_err` executables: 115 checks across their 18 groups,
with zero failures. The host validator reported
`run_kernel validation passed: mode=netlink-route-netns`.
The normalized [QEMU transcript](2026-09-24-netlink-route-netns-qemu.log) has
SHA256 `d2fc4493414183cc3a1ba22b85d5208e8a14e2f1c541eb51aa464169cbb68666`.
`python3 -m unittest -q tools.riscv.tests.test_validate_run_kernel_log`
passed 28 tests. `make kernel TARGET_ARCH=x86_64` completed and produced an
ISO with SHA256 `ce2aa14da75cd69047d9b0e9f81304f8837b3b02668237dea2b6b6fb2344557a`.

The shared volume had no ordinary-user free blocks. For each build, a local
shell trap temporarily reduced the ext4 reserved-block count from 24418932
to 23000000 and restored it on exit; the original count was verified after
the final build. The generated 2.2 GiB x86 target directory was removed
after verification. This storage workaround is not part of the kernel change.

The netlink port table and kernel route socket remain global. This regression
establishes the namespace used for route requests; it does not establish
per-namespace port-ID reuse or multicast isolation. Those require separate
tests and changes.
