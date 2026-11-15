# Honor `MSG_DONTWAIT` on UDP receives

A blocking IPv4 UDP socket with an empty receive queue waited inside
`recvfrom(..., MSG_DONTWAIT)` and `recvmsg(..., MSG_DONTWAIT)` instead of returning
`EAGAIN`. The [regression test](../../../../test/initramfs/src/regression/network/udp_msg_dontwait.c)
uses a child with a two-second alarm so a blocking call fails promptly. It
passes on the Linux host.

The receive path now calls `try_recv` directly when the per-call flag is set.
Without the flag, it retains the existing `block_on` path and receive timeout.
The test is part of the full network regression and has a focused QEMU entry:

```sh
make run_kernel AUTO_TEST=udp_msg_dontwait TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
```

The [QEMU observations](qemu-observations.txt) show the test failing on parent
commit `ce2faed5a` because the child was killed by `SIGALRM`, then passing with
the fix. The adjacent UDP user-buffer prefault regression also passed with the
same kernel image, SHA-256
`d4644dce863553ab4737ad88b48811598666b268776ec4d3c16231f6c3cd888d`.
The native LMBench ALL suite passed 109/109 on grandparent PR #158; it was not
rerun for this focused change.
