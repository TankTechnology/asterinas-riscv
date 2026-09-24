# Defer socket receive faults until data reaches the invalid page

The UDP and TCP receive-capacity fixes bound prefaulting to 64 and 128 KiB,
respectively. They still rejected a one-byte receive if a later page within
that bound was inaccessible. The [Linux oracle](linux-oracle.py) shows that
recvfrom, recvmsg, and read all return the one byte when the first 4 KiB
is writable and the next page is inaccessible. A 5,000-byte UDP packet
that crosses that page instead returns EFAULT. Its
[output](linux-oracle-output.txt) retains the exact results.

The existing UDP and TCP QEMU regressions were extended to use a 10 MiB
buffer twice: first with the full per-socket receive capacity writable,
then with only its first 4 KiB writable. Before the change, recvfrom and
read returned EFAULT in the second pass for both socket types. Afterward,
all three receive APIs passed; an additional UDP receive still checks
that data crossing the invalid page returns EFAULT. The
[red and green observations](qemu-regression-observations.txt) preserve
the markers. The focused RISC-V QEMU commands were:

    make run_kernel AUTO_TEST=udp_user_buffer_prefault TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
    make run_kernel AUTO_TEST=tcp_user_buffer_prefault TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1

A single-buffer writer now uses the same page-by-page prefault and deferred
copy fault as the existing iovec writer. When prefaulting encounters an
inaccessible page after a valid prefix, the writer keeps that prefix;
a copy that reaches the fault returns a page error. This applies to
recvfrom and socket read. The socket read helper is shared with the
FileLike implementation, keeping empty-buffer behavior consistent.
The per-socket maximum receive capacities remain in effect.

## Native LMBench after the change

The pinned asterinas/lmbench revision
afb47eddaf10a411c1ea3cb64965461f1308a6ea ran its native
cd /opt/lmbench/src && make results command in Debian RISC-V QEMU.
The packaged GNUmakefile delegates to the original Makefile while
skipping in-guest compilation of already-built benchmark binaries.
The [QEMU summary](qemu-summary.json) reports physical=false, a passing
gate, and kernel image SHA256
2b65f6e555ee1befeb7ea2c77ce5d9e6e2e9baf1a16e6fcd741316f40bf2ac0b.

The native driver [report](report.json) records 109/109 selected
measurement groups, no missing groups, errors, or metadata warnings,
and 572.095 seconds elapsed. The [raw results](native-results.txt)
include UDP latency using localhost: 311.0039 microseconds and TCP
latency using localhost: 220.2117 microseconds. The
[configuration](CONFIG), [status](status.log), [lifecycle events](lifecycle-events.jsonl),
and [host runner log](runner-host.log) retain the source evidence.
These are QEMU measurements, not development-board performance scores.

The desktop-ready systemd unit failed because the QEMU command line did
not arm its software-reboot watchdog. The gate separately observed
active graphical and desktop services and the benchmark completed.

The TCP transport can report short progress after a copy fault. This
change does not alter that transport policy; behavior when TCP data
itself crosses an inaccessible page needs separate compatibility testing.
