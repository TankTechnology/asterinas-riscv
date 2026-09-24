# Bound UDP receive prefaulting to the packet capacity

A focused RISC-V QEMU experiment isolated the anomalous native LMBench UDP
latency to user-buffer prefaulting. The original kernel prefaulted the entire
requested receive buffer before a packet could be returned. LMBench's UDP
server supplies a roughly 10 MiB receive buffer for a tiny packet, so the
kernel visited about 2,560 user pages for each receive. The fixed
aster-bigtcp UDP receive buffer holds at most 65,536 bytes per operation.

The reproducer in [udp-recv-buffer-timelines.json](udp-recv-buffer-timelines.json)
sent 20 four-byte loopback messages with otherwise identical Python socket
operations. Excluding the first cold exchange, the median round trip was
298.1 microseconds with a four-byte receive buffer and 32,513.3 microseconds
with a 10 MiB receive buffer. In the latter case, 32,232.2 microseconds fell
between the client send and the server's receive return.

The fix exposes a per-socket maximum receive length and uses it to bound
prefaulting in recvfrom, recvmsg, and socket read. Other sockets retain
their existing prefault behavior. An existing UDP regression now also checks
a 10 MiB mapping whose first 64 KiB is writable and whose tail is inaccessible.
Before the fix, recvfrom and read returned EFAULT on this test; after the
fix, all three receive interfaces passed. The same C program also passed on
the Linux host. The focused QEMU regression command was
make run_kernel AUTO_TEST=udp_user_buffer_prefault TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1.

The unchanged pinned asterinas/lmbench lat_udp binary
(SHA256 a57f785a430d194589c887218182f2ecbaad2b574ef31d89dbb49f537176303e)
was run in the same Debian full-system QEMU environment with
lat_udp -P 1 -W 0 -N 3 localhost. The measured latency dropped from
34,003.1290 to 327.4862 microseconds, a 103.8-fold improvement.
[focused-lat-udp.json](focused-lat-udp.json) records both immutable kernel
image hashes and the exact command. A separate Ubuntu RISC-V full-system
QEMU control measured 97.6223 microseconds with the same benchmark binary;
that different guest and kernel provide context, not a like-for-like speed
comparison.

This is a bounded prefault optimization. It still prefaults up to 64 KiB for
a short UDP message, so an inaccessible page inside that range may still
cause recvfrom or read to fail before reaching the actual message.

## Native suite after the fix

The pinned asterinas/lmbench revision
afb47eddaf10a411c1ea3cb64965461f1308a6ea ran its own
cd /opt/lmbench/src && make results command in Debian RISC-V QEMU. Its
packaged GNUmakefile delegates to the original Makefile with the existing
binary marked up to date, so there is no in-guest compilation. The
[QEMU summary](qemu-summary.json) records the fixed kernel image
SHA256 6662070404f0ef93dcdafc28f32f713665ab980b1a30f35a5d51310aadf8d261,
physical=false, and a passing gate. The root debug console returned
nonce-framed UID 0 responses from the same boot.

The native driver exited zero and [reported](report.json) 109/109 selected
measurement groups, zero missing groups, errors, or metadata warnings,
and 595.627 seconds elapsed. The [raw results](native-results.txt) include
UDP latency using localhost: 321.4663 microseconds. The
[configuration](CONFIG), [status](status.log), [lifecycle events](lifecycle-events.jsonl),
and [host runner log](runner-host.log) retain the source evidence.

The desktop-ready systemd unit failed because this QEMU boot did not arm
its software-reboot watchdog. The gate separately observed active graphical
and desktop services and the benchmark completed; this is a QEMU test,
not a development-board readiness or performance result.
