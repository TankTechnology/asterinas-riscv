# Bound TCP receive prefaulting to the receive ring capacity

After the UDP prefault fix, IP/TCP still resolved every page of an
application-provided receive buffer before entering the network stack. A
focused RISC-V full-system QEMU experiment used the same Debian root image,
one-byte loopback TCP exchanges with TCP_NODELAY, and receive capacities
of 1 byte, 10 MiB, then 1 byte again. Each case had 16 exchanges; medians
exclude the first cold exchange. The [raw timelines](tcp-buffer-timelines.json)
show these median round-trip times:

| Kernel | 1-byte buffer | 10 MiB buffer | 1-byte buffer again |
| --- | ---: | ---: | ---: |
| Before | 311.5 us | 33,250.9 us | 283.6 us |
| After | 298.9 us | 597.1 us | 296.8 us |

Before the fix, 33,001.7 us of the 10 MiB case fell between the client
send and the server receive return. Afterward, that interval was 353.0 us.
The unchanged small-buffer cases control for drift across the two boots.

The TCP socket uses a fixed 128 KiB receive ring. One stream read can span
two contiguous ring slices, but cannot return more bytes than the ring
holds. StreamSocket now reports that maximum through the same per-socket
receive-length interface introduced for UDP. The syscall prefault paths
therefore stop after 128 KiB even when userspace requests more. Other socket
kinds and small buffers keep their earlier behavior.

The existing TCP user-buffer regression now includes a 10 MiB mapping
whose first 128 KiB is writable and whose tail is inaccessible. Its
recvfrom and read operations returned EFAULT before the fix; the same
RISC-V QEMU test passed after the fix. The command was
make run_kernel AUTO_TEST=tcp_user_buffer_prefault TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1.

This remains bounded prefaulting, not exact demand faulting. A page fault
inside the first 128 KiB can still be reported before a shorter read reaches
that page.

The exact [QEMU regression observations](qemu-regression-observations.txt)
retain the before and after markers. An isolated
[Linux host oracle](linux-oracle.py) independently returned one byte
from the same protected-tail pattern through recvfrom, recvmsg, and read;
its [output](linux-oracle-output.txt) is recorded. The older Unix socket
portion of the Asterinas C regression has different Linux-host behavior,
so that full regression was not used as the Linux oracle.

## Native LMBench after the TCP fix

The pinned asterinas/lmbench revision
afb47eddaf10a411c1ea3cb64965461f1308a6ea ran its own
cd /opt/lmbench/src && make results command in Debian RISC-V QEMU.
As in the previous stack run, its packaged GNUmakefile delegates to the
original Makefile with the binaries already built; there was no guest
compilation. The [QEMU summary](qemu-summary.json) reports a passing
full-system gate, physical=false, and fixed kernel image SHA256
320c133bc50a0ad164c30faa2f9db384924b48bf560fb54b4cbf7cbcec2ac8c1.
The root debug console returned nonce-framed UID 0 responses for this boot.

The native driver [report](report.json) says 109/109 selected measurement
groups, zero missing groups, errors, or metadata warnings, and an elapsed
time of 595.854 seconds. The [raw results](native-results.txt) include
TCP latency using localhost: 217.8796 microseconds and UDP latency using
localhost: 358.7247 microseconds. The [configuration](CONFIG),
[status](status.log), [lifecycle events](lifecycle-events.jsonl), and
[host runner log](runner-host.log) preserve the underlying evidence.
These single-run latencies are QEMU measurements, not hardware scores.

The desktop-ready systemd unit again failed because this QEMU boot had no
software-reboot watchdog armed. The gate separately observed active
graphical and desktop services and the benchmark completed; this does not
qualify development-board readiness.
