# Honor `MSG_DONTWAIT` on TCP sends

On a blocking TCP socket with a full send buffer, `sendmsg(..., MSG_DONTWAIT)`
waited for writable space instead of returning `EAGAIN`. The
[regression test](../../../../test/initramfs/src/regression/network/tcp_msg_dontwait_send.c)
first fills the send buffer in nonblocking mode, restores the socket's blocking
mode, then uses the per-call flag. Its child has a four-second alarm so the old
blocking behavior fails promptly. The same test passes on the Linux host.

The TCP send path now calls `try_send` directly when `MSG_DONTWAIT` is set.
Calls without it retain the existing blocking and send-timeout behavior. The
test is part of the full network regression and has a focused QEMU entry:

```sh
make run_kernel AUTO_TEST=tcp_msg_dontwait_send TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
```

The [QEMU observations](qemu-observations.txt) show a red run on parent commit
`42592e1e5`, followed by a green run and a passing adjacent TCP user-buffer
regression with the fix. The final kernel image SHA-256 is
`bf644bbd786c3e05dd71663daa5462dd51f853f21dc22a23896df09732f86c6d`.
The native LMBench ALL suite passed 109/109 on ancestor PR #158 and was not
rerun for this focused fix.
