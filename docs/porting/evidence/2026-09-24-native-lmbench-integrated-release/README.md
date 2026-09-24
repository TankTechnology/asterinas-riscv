# Native LMBench ALL on the integrated release Image

The combined LMBench and ICMP branch at `5e5e167d6d1549e02b16c72b0bcb450f58e99283`
was built in the persistent project container with
`make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1`.
The RISC-V release Image SHA-256 was
`43ee67d0c18c88e2314e2867c624d47cdcfbb61555e8435b0a1e77946f8b74f7`.
The [QEMU result](qemu-summary.json) binds that Image to the Debian root,
Stage1, DTB, U-Boot, manifest, package lock, and package checksums.

The runtime archive SHA-256 was
`54bf3c45ca425b8736e54e9723ff50d6c4c306af26ff033c25f89c4272236af3`.
`python3 tools/riscv/lmbench_native.py verify --archive` passed before boot,
confirming the pinned `asterinas/lmbench` revision
`afb47eddaf10a411c1ea3cb64965461f1308a6ea`, the RISC-V platform,
the native make entry, and the two declared script adaptations.
The guest used the native `cd /opt/lmbench/src && make results` command;
the bundled singular `make result` target is an alias to the same runner,
as checked in the [integration entry audit](../2026-09-24-lmbench-icmp-integration/make-entry-check.txt).
Only compilation was skipped because the archive supplies prebuilt binaries.

The native command and outer QEMU driver exited zero.
The [native audit](report.json) found **109/109 distinct measurement groups**
in 569.25 seconds, with no missing groups, errors, metadata warnings, or timeout.
The observed group names and binary hash map match the earlier
[LMBench qualification](../2026-09-24-native-lmbench-no-egrep/README.md).
Both small network regression preflights passed before the suite.
The [raw native results](native-results.txt), [configuration](CONFIG),
[status log](status.log), [stdout](stdout.log.gz), [stderr](stderr),
[lifecycle events](lifecycle-events.jsonl), and
[serial capture](qemu-serial.log.gz) preserve the measurements and exact
execution evidence.
The scratch [host driver](host-driver.py) and
[guest driver](guest-driver.py) are retained for replay;
they are evidence helpers rather than installed benchmark entry points.

This is the fork's native ALL selection with one copy, 8 MiB, FASTMEM,
local networking, and file-system tests; it does not select every standalone
benchmark executable, raw disks, or remote hosts.
QEMU TCG results establish compatibility, not a physical-board or Linux
performance comparison.
Keep the separate 18-case smoke as the inexpensive daily check; the native
ALL run is a bounded closeout qualification.
