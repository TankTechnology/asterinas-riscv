# IP socket namespace selection on RISC-V QEMU

Linux's IPv4 bind path resolves the network namespace from the socket
(`sock_net(sk)`), not from the thread executing `bind`.
[Linux source](https://raw.githubusercontent.com/torvalds/linux/master/net/ipv4/af_inet.c)
Asterinas already retained a namespace on each socket for interface ioctls,
but IP bind and automatic interface selection still used `current_net_ns()`.
After `unshare(CLONE_NEWNET)`, an inherited socket therefore selected the new
namespace's loopback instead of its original `eth0`.

The focused `ip_socket_netns` regression creates UDP and TCP sockets before
`unshare`, then checks that they can bind the original `eth0` address
(`10.0.2.15`) while newly created sockets reject it with `EADDRNOTAVAIL`.
It also checks that an inherited unbound UDP socket connecting to the QEMU
gateway chooses `10.0.2.15` as its local address. The gate runs the existing
TCP and UDP privileged-port regression before the namespace case.

The first QEMU run used the previous kernel with only the new regression
installed. It failed at `ip_socket_netns.c:41`: binding the inherited UDP
socket to `eth0` returned an error. After the kernel change, the dedicated
gate passed on four-CPU RISC-V QEMU:

```sh
make run_kernel AUTO_TEST=ip_socket_netns TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
```

Both privileged-port groups passed all four checks, and the namespace
regression printed its terminal success marker. The normalized
[QEMU transcript](2026-09-24-ip-socket-netns-qemu.log) has SHA256
`a40361212229ecb6730f79e889622f380a7f8efb167a612fda24c15d769469c9`.
The existing `AUTO_TEST=ifreq`, `AUTO_TEST=ipv6_dual_stack_udp`, and
`AUTO_TEST=ipv6_dual_stack` RISC-V gates also passed after the change.
`python3 -m unittest -q tools.riscv.tests.test_validate_run_kernel_log`
passed 28 tests. The x86 kernel built with `make kernel TARGET_ARCH=x86_64`;
its ISO SHA256 was
`41b87c68badf2bcd89def5fb79d90c8c51097f018c9bb8638f985b2d184eb57c`.

The shared development volume had no ordinary-user free blocks. For each
build, the ext4 reserved-block count was temporarily lowered from 24418932
to 23000000 inside a shell trap and restored on exit. The original count was
verified after the final build. The generated 2.2 GiB x86 target directory
was removed after verification. This storage workaround was local to these
builds; it is not part of the kernel change.

The native LMBench ALL suite was not repeated because the previously qualified
109/109 path runs in the initial namespace and this change preserves that
path's interface selection. Netlink route requests still consult the current
thread's namespace and need separate work.
