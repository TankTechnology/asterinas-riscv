# Native LMBench ALL on the current network stack

The RISC-V QEMU kernel at commit `89d669939d42f0a367c3b065ba7521d077f605e0`
has image SHA-256 `e106f84051f053929131f2cf21c1eae23c4c3383c88b52f20b11a833b63d1089`.
The [QEMU summary](qemu-summary.json) records that exact input, Debian 13.7,
a working UID-0 debug console, and `physical=false`.

The pinned `asterinas/lmbench` revision
`afb47eddaf10a411c1ea3cb64965461f1308a6ea` ran its own
`cd /opt/lmbench/src && make results` entry point. The packaged runtime
SHA-256 was `e65c52261a1483c12196840a7d15f70ea272bad7d0dc66ad3d10b72552961087`.
Its GNUmakefile delegates to the original Makefile with `-o lmbench`, using
prebuilt binaries so the guest does not need to compile. Script adaptations
select IPv4 loopback, use `grep -E`, and skip modern `netstat -i` column
headings. The guest kept the serial console idle for the first 180 seconds
of the suite to avoid perturbing local networking measurements.

The native command and outer QEMU driver both exited zero. The
[report](report.json) found **109/109 measurement groups**, no missing groups,
errors, or metadata warnings, in 580.875 seconds. The guest first passed the
reserved-ports regression (5/5) and UDP-loopback regression (2/2). The
[raw results](native-results.txt), [configuration](CONFIG), [status log](status.log),
and [lifecycle events](lifecycle-events.jsonl) retain the measurements and
execution evidence.

This is the compact native ALL profile: 8 MiB, one copy, FASTMEM, local
networking, no raw disks or remote hosts. These are QEMU TCG compatibility
results, not development-board performance scores or coverage of every
LMBench executable. The full run is an on-demand milestone check; focused
QEMU regressions remain the inexpensive daily gate.
