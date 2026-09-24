# Preserve queued TCP data when a receive copy faults

The preceding socket-prefault change deferred a user-buffer fault until the
incoming data reached the inaccessible page. TCP still returned a short read
and consumed the copied prefix when a copy failed midway. This is visible with
`recvmsg` into a one-byte valid iovec followed by an inaccessible iovec: the
old Asterinas result was one byte, leaving only the suffix for a retry.

The [Linux loopback oracle](linux-oracle.c) and its
[output](linux-oracle-output.txt) show the expected boundary: a receive whose
data fits in the valid prefix succeeds, while a receive that reaches the
inaccessible page returns `EFAULT` and leaves the full message queued. The
oracle covers `read`, `recvfrom`, and one- and two-iovec `recvmsg` calls. It
ran on Linux 6.5.0-15-generic x86_64.

The TCP receive callback now tells smoltcp to dequeue zero bytes when the
copy fails. Already completed earlier contiguous ranges still count as
progress. The focused regression checks both a three-byte split-iovec fault
and a 5,000-byte payload crossing the 4 KiB writable prefix through
`recvfrom`, `recvmsg`, and `read`. After each `EFAULT`, it retries into a valid
buffer and checks every byte.

Before the fix, the changed regression failed in QEMU with
`recvmsg partial iovec EFAULT: Success`. After the fix, these RISC-V QEMU
commands passed with `SMP=4` and `FEATURES=riscv_sv39_mode`:

    make run_kernel AUTO_TEST=tcp_user_buffer_prefault TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
    make run_kernel AUTO_TEST=tcp_ppoll_wakeup TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
    make run_kernel AUTO_TEST=tcp_event_handoff TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1

The [QEMU observations](qemu-regression-observations.txt) retain the red and
green markers; the tested [kernel image SHA256](qemu-kernel-image.sha256)
identifies the artifact. This run used QEMU, not the development board. The
full native LMBench suite was not repeated for this narrow fault-path change;
the parent change's native driver recorded 109/109 selected groups.

The current TCP transport still reports short progress when an earlier
contiguous ring-buffer range was successfully consumed before a later range
faults. That wrap-boundary case needs a separate Linux comparison and is not
covered by this regression.
