# Native LMBench ALL on the final network candidate

Source commit `60c67b8c46a88b76b3441b3a3b86bb9a99bda0e7` includes the
LMBench/ICMP integration, the TCP and UDP network corrections, and the full
RISC-V network regression harness. Its release Image SHA-256 was
`26835fc6fef49418d83d524a3da25a36b00b09c18352a03feeb914e13ec6be79`.
The [full network regression](../2026-09-24-network-full/README.md) passed
on that same Image. The [QEMU result](qemu-result.json) binds the native run
to this Image and the Debian root, Stage1, DTB, U-Boot, manifest, package
lock, and package checksums.

The existing self-contained runtime archive passed
`python3 tools/riscv/lmbench_native.py verify --archive` before boot. Its
SHA-256 was `54bf3c45ca425b8736e54e9723ff50d6c4c306af26ff033c25f89c4272236af3`;
it packages [asterinas/lmbench](https://github.com/asterinas/lmbench) revision
`afb47eddaf10a411c1ea3cb64965461f1308a6ea`. The guest ran
`cd /opt/lmbench/src && make results` with prebuilt RISC-V binaries, skipping
only compilation. The native `make result` alias was audited earlier in the
[integration entry check](../2026-09-24-lmbench-icmp-integration/make-entry-check.txt).

The native command and outer QEMU driver both exited zero. The
[native report](report.json) records **109/109 distinct measurement groups**
in 582.207 seconds, with no missing groups, errors, metadata warnings, or
timeout. The observed group list and binary SHA-256 map exactly match the
[earlier merged-baseline qualification](../2026-09-24-native-lmbench-merged-baseline/README.md).
The two small guest network preflights passed 5/5 and 2/2 assertions. The
[raw results](native-results.txt), [configuration](CONFIG),
[status](status.log), [stdout](stdout.log.gz), [stderr](stderr),
[lifecycle events](lifecycle-events.jsonl), and
[serial transcript](serial.log.gz) preserve the run. The same scratch
[host](../2026-09-24-native-lmbench-integrated-release/host-driver.py) and
[guest](../2026-09-24-native-lmbench-integrated-release/guest-driver.py)
drivers were used for replay; they are evidence helpers rather than the
installed benchmark entry point.

This is the fork's native ALL selection with one copy, 8 MiB, FASTMEM,
loopback networking, and filesystem tests. It does not select every
standalone benchmark executable, raw disks, or remote hosts. QEMU TCG timings
establish compatibility, not physical-board or Linux performance parity.
