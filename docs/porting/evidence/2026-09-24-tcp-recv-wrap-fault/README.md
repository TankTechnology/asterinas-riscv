# Reject a known TCP receive fault before the ring wraps

The preceding TCP copy-fault change leaves the current contiguous receive
range queued on a user-copy error. A 128 KiB receive ring can still wrap during
one logical read: the first range is consumed successfully, then a later range
reaches an inaccessible user page. Before this change, the focused QEMU test
returned a successful 3,000-byte short read and lost those bytes from the
retry.

The [Linux oracle](linux-split-oracle.c) sends a 5,000-byte message in 3,000-
and 2,000-byte TCP writes with `TCP_NODELAY`, waits until `FIONREAD` confirms
all 5,000 bytes are queued, then reads into a buffer with only its first 4 KiB
writable. Its [output](linux-split-oracle-output.txt) shows `EFAULT` and an
unchanged 5,000-byte queue for `read`, `recvfrom`, and both `recvmsg` layouts.
The oracle ran on Linux 6.5.0-15-generic x86_64.

The QEMU regression primes Asterinas's receive-ring cursor to 3,000 bytes
before its end, then sends a 5,000-byte payload. A user buffer has only 4 KiB
writable. The TCP layer now checks the whole queued length against a known
prefault error boundary while holding the receive lock, before consuming
either ring range. A buffer without a known deferred fault only takes the
existing copy path. The same test checks `MSG_PEEK` and `recvfrom`, then retries
and verifies all 5,000 bytes. It asserts the returned error and queue contents,
not whether an error left part of the user buffer modified.

Before the change, the focused RISC-V QEMU test reported
`TCP wrapped cross-fault: received=3000 errno=0`. After the change, these
release-mode QEMU commands passed with `SMP=4` and `riscv_sv39_mode`:

    make run_kernel AUTO_TEST=tcp_user_buffer_prefault TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
    make run_kernel AUTO_TEST=tcp_ppoll_wakeup TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
    make run_kernel AUTO_TEST=tcp_event_handoff TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
    make run_kernel AUTO_TEST=udp_user_buffer_prefault TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1

The [red and green observations](qemu-regression-observations.txt) and tested
[kernel image SHA256](qemu-kernel-image.sha256) identify the run. This is a
QEMU result, not a development-board result. The full native LMBench driver
was not rerun for this narrow fault path; the earlier parent stack recorded
109/109 selected groups.

The test covers a prefaulted user buffer with a known inaccessible boundary.
Socket `readv` currently uses the generic per-iovec file path, and the TCP
`MSG_PEEK` implementation still exposes only the first contiguous ring range
when no fault is known. Those are separate compatibility questions.
