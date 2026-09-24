# Receive socket `readv` as one message

Socket `readv` used the generic file path, calling `file.read` once per iovec.
With one writable byte followed by an invalid iovec, the first call consumed
one TCP byte and the syscall returned a successful short read instead of
reporting the fault. This also bypassed the socket receive prefault and
deferred-fault handling used by `recvmsg`, `recvfrom`, and `read`.

The [Linux TCP oracle](linux-readv-oracle.c) and its
[output](linux-readv-oracle-output.txt) show `readv` returning `EFAULT` while
the queue retains all three bytes in the split-iovec case, and all 5,000
bytes when a single buffer crosses a 4 KiB writable prefix. The expanded UDP
regression also [passed on Linux](linux-udp-oracle-output.txt). These Linux checks
ran on 6.5.0-15-generic x86_64.

The socket branch of `readv` now prefaults the whole iovec array up to the
socket's maximum receive length and invokes the shared socket receive helper
once. The generic per-iovec file path remains in place for non-sockets. The
TCP regression sends `uvw`, checks that the split-iovec call returns `EFAULT`,
and retries to verify all three bytes; it also adds a 5,000-byte cross-page
`readv` call. The UDP regression exercises `readv` with a short message and a
packet that reaches the inaccessible page.

The [QEMU red/green observations](qemu-regression-observations.txt) show the
original `readv partial iovec EFAULT: Success` failure and the passing tests
after the fix. The RISC-V QEMU commands were:

    make run_kernel AUTO_TEST=tcp_user_buffer_prefault TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
    make run_kernel AUTO_TEST=udp_user_buffer_prefault TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1

The [kernel image SHA256](qemu-kernel-image.sha256) identifies the tested
artifact. These are QEMU tests, not board tests. The full native LMBench ALL
driver was not repeated for this focused syscall change; an earlier parent
stack recorded 109/109 selected measurement groups.
