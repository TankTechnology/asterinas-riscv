# Native LMBench ALL on the merged baseline fixes

The integrated source `0bd1102cdbcbe4b57844b2a3f0fbc0ee8c195cfe`
contains the native LMBench and ICMP stack, the workspace rustfmt fix, and
the cross-architecture build fix. The persistent project container built
its RISC-V release kernel with
`make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1`.
The resulting Image SHA-256 was
`abd5bbc0f62085553a9da630f3b174495bbd6df53f1336d0d08ff94d1f8f2908`.
The [QEMU result](qemu-result.json) binds that Image to the Debian root,
Stage1, DTB, U-Boot, manifest, package lock, and package checksums.

The unchanged native runtime archive SHA-256 was
`54bf3c45ca425b8736e54e9723ff50d6c4c306af26ff033c25f89c4272236af3`.
`python3 tools/riscv/lmbench_native.py verify --archive
target/lmbench-native-20260923/lmbench-runtime-no-egrep.tar.gz` passed before
the run, confirming the pinned `asterinas/lmbench` revision
`afb47eddaf10a411c1ea3cb64965461f1308a6ea`, RISC-V platform, and
declared script adaptations. The guest used the native
`cd /opt/lmbench/src && make results` entry with prebuilt binaries;
compilation was the only skipped Makefile step. The singular `make result`
alias was checked separately in the
[integration entry audit](../2026-09-24-lmbench-icmp-integration/make-entry-check.txt).

Both focused network regression preflights passed. The native command and
outer QEMU driver exited zero. The [native report](report.json) records
**109/109 distinct measurement groups** in 579.949 seconds, with no
missing groups, errors, metadata warnings, or timeout. The observed group
list and binary hash map exactly match the
[earlier integrated qualification](../2026-09-24-native-lmbench-integrated-release/README.md).
The [raw results](native-results.txt), [configuration](CONFIG),
[status](status.log), [stdout](stdout.log.gz), [stderr](stderr),
[lifecycle events](lifecycle-events.jsonl), and
[serial transcript](serial.log.gz) retain the execution evidence. The same
scratch [host](../2026-09-24-native-lmbench-integrated-release/host-driver.py)
and [guest](../2026-09-24-native-lmbench-integrated-release/guest-driver.py)
drivers were used for replay; they are not installed benchmark entry points.

This is the fork's native ALL selection with one copy, 8 MiB, FASTMEM,
local networking, and filesystem tests. It does not select every standalone
benchmark executable, raw disks, or remote hosts. QEMU TCG timings establish
compatibility, not physical-board or Linux performance parity. The
[18-case replay](../2026-09-24-lmbench-icmp-integration/merged-baseline/README.md)
remains the inexpensive daily check.
