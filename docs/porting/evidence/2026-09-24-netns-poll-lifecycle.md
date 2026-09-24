# Releasing a network namespace's loopback poll thread

Each new network namespace creates a private loopback interface and starts a
background polling thread. The thread retained an `Arc<Iface>` and its loop had
no exit condition. After the last namespace and socket reference disappeared,
the thread still held the interface and waited indefinitely.

A focused kernel test creates a child namespace, keeps a weak reference to its
loopback interface, drops the namespace, yields to the polling thread, and
requires the weak reference to expire. With only this test added to the base
kernel, the RISC-V QEMU test failed at `loopback interface leaked` (0 passed,
1 failed). The first two attempts to prepare the test did not reach that
assertion because the shared disk was full and the kernel-test clock fixture
had not been initialized; neither is counted as evidence for the bug.

On namespace drop, the poll scheduler now records a stop request and wakes its
wait queue. The poll thread checks that request both while waiting for the next
poll and while waiting for a scheduled deadline, then exits and releases its
interface reference. The initial namespace remains a static singleton. The
stop request does not change packet delivery or interface configuration.

The final source includes the netlink visibility follow-up in the parent PR.
The short RISC-V QEMU checks were:

```sh
cd kernel
OSDK_TARGET_ARCH=riscv64 cargo osdk test child_netns_poll_thread_releases_loopback --release --scheme riscv --features=riscv_sv39_mode --grub-boot-protocol=multiboot2
cd ..
make run_kernel AUTO_TEST=ip_socket_netns TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
make run_kernel AUTO_TEST=netlink_route_netns TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
make run_kernel AUTO_TEST=tcp_event_handoff TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
```

The focused kernel test passed 1/1 (244 filtered). The IP gate passed its
namespace regression and 8 privilege checks. The netlink gate passed both
namespace regressions and 182 checks across 30 groups with no failures. The
TCP gate completed 4096 socket-to-queue-to-pipe handoffs. Both network-log
validators passed. The normalized transcripts are:

- [Kernel test](2026-09-24-netns-poll-lifecycle-ktest.log), SHA256 `a6f3b53c46dfeeca55268ecc174767ce932c03dad22f13c7ec9a5429d2f0d7df`.
- [IP gate](2026-09-24-netns-poll-lifecycle-ip.log), SHA256 `73972d202486084b4d2fb2dea0ea0730ecf87875fed5194a9eca01d0343674d0`.
- [Netlink gate](2026-09-24-netns-poll-lifecycle-netlink.log), SHA256 `772a97cac74b5de14d5848611ab19807c0a301a62bc708b474871ce1d76d402e`.
- [TCP gate](2026-09-24-netns-poll-lifecycle-tcp.log), SHA256 `cd3e99b163162a390e4c77b024b4fa29dd6d819fca67b2bd581ef123d8cf2b36`.

`make kernel TARGET_ARCH=x86_64` also completed; its ISO SHA256 was
`dbd892acb807956eca800d83ea00be1e702aef3caacdb3d2a7cd491640ce2a33`.
The generated x86 target directory was removed after hashing the ISO. A local
build wrapper temporarily reduced the ext4 reserved-block count and restored
its original value, `24418932`, after each run.

This test proves reclamation after a quiescent child namespace closes. The
existing QEMU gates exercise namespace creation, interface requests, and
ordinary TCP wakeups, but do not exhaustively cover all teardown races. The
native LMBench ALL run was not repeated; its previously qualified 109/109
result was on the earlier kernel and is not a new measurement for this change.
No physical board was used.
