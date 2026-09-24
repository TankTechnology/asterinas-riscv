# Per-network-namespace netlink port tables on RISC-V QEMU

Linux keys a netlink port by the socket's network namespace as well as its
port ID. Its lookup and insertion paths use the socket namespace.
[Linux source](https://github.com/torvalds/linux/blob/master/net/netlink/af_netlink.c)
Asterinas previously kept one global ROUTE table and one global UEVENT table.
As a result, a socket created after `unshare(CLONE_NEWNET)` could not bind the
same explicit port ID as a socket in the original namespace. Multicast group
membership also shared the global tables.

The route regression now assigns the same explicit port ID to sockets created
before and after `unshare`; both must receive their own `RTM_GETLINK` and
`RTM_GETADDR` replies. A new UEVENT regression binds the same port ID and
group in both namespaces, while verifying that a duplicate inside one
namespace still fails with `EADDRINUSE`. With only these tests installed, the
old kernel bound the first route socket, then failed to bind the second:

```text
pre-unshare route socket bound
route socket bind failed: -6
netlink_route_netns.c:24: route_socket: Assertion `result == 0' failed.
```

The ROUTE and UEVENT tables are now owned by each `NetNamespace`. A bound
socket retains its table, and reply delivery and group operations use that
same table. The synthetic UEVENT kernel test creates two independent tables,
binds the same port and group in both, broadcasts to one, and verifies that
only its queue receives the message.

The final short QEMU gate was:

```sh
make run_kernel AUTO_TEST=netlink_route_netns TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
```

It passed both namespace regressions plus the existing `netlink_route`,
`rtnl_err`, and `uevent_err` executables: 182 checks across 30 groups, with
zero failures. The complete-log validator passed for
`mode=netlink-route-netns`. The normalized
[QEMU transcript](2026-09-24-netlink-table-netns-qemu.log) has SHA256
`a8d1f8c12b578492119b890837541745843e0e7eebfa36a45bb4a51c197aa1b5`.

The focused RISC-V kernel test command, run inside the persistent development
container from the `kernel/` directory, was:

```sh
OSDK_TARGET_ARCH=riscv64 cargo osdk test multicast_synthetic_uevent --release --scheme riscv --features=riscv_sv39_mode --grub-boot-protocol=multiboot2
```

It reported one test passed, zero failed, and 243 filtered out. The normalized
[kernel-test serial transcript](2026-09-24-netlink-table-netns-ktest.log)
has SHA256
`14974d3874042ba929e332a831a5808d106ec2a47a21411ce52cab72c4016016`.
`python3 -m unittest -q tools.riscv.tests.test_validate_run_kernel_log`
passed 29 tests. `make kernel TARGET_ARCH=x86_64` completed; its ISO SHA256
was `a035bc196634b598c7ea79e2560bbe4f1c3d7526e9f7ad0ff672bd0b9e356d69`.

The shared development volume had no ordinary-user free blocks. A local
shell trap temporarily reduced the ext4 reserved-block count from 24418932
to 23000000 for each build and restored it on exit. The original value was
verified after the final run, and the generated 2.2 GiB x86 target directory
was removed after verification. This is a local build workaround, not a
kernel change.

The synthetic kernel test proves table-level multicast isolation. Asterinas
currently has no production UEVENT broadcast path, so end-to-end multicast
delivery across namespaces remains untested. The native LMBench ALL suite
was not repeated because its qualified 109/109 path runs in the initial
namespace and this change affects only namespace scoping of netlink tables.
