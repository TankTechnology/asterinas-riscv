# Native LMBench ALL on the final IPv4 route-dump kernel

The RISC-V QEMU kernel built from the `codex/route-local-table-current-stack`
branch has Image SHA-256
`3e3707b7e59f46303a914395a8dd4a85de55d0feb063fa26bb510130ce02b40e`.
This is the same versioned Image used in the two Megrez board boots documented
in [the route-dump evidence](../2026-09-24-route-local-table-current-stack/README.md).
The [QEMU summary](qemu-summary.json) records this input, Debian 13.7,
an active UID-0 debug console, `physical=false`, and a passing outer driver.

The pinned `asterinas/lmbench` revision
`afb47eddaf10a411c1ea3cb64965461f1308a6ea` ran its own
`cd /opt/lmbench/src && make results` entry point. The runtime archive SHA-256
was `e65c52261a1483c12196840a7d15f70ea272bad7d0dc66ad3d10b72552961087`.
Its GNUmakefile delegates to the original Makefile with `-o lmbench`, using
prebuilt RISC-V binaries. The three recorded script adaptations select IPv4
loopback servers, use `grep -E`, and skip modern `netstat -i` column headings.
The guest left the serial console idle for the first 180 seconds to avoid
perturbing local networking measurements.

The native command and QEMU driver both exited zero. The [audit report](report.json)
found **109/109 measurement groups** in 574.417 seconds, with no missing groups,
errors, or metadata warnings. The [raw results](native-results.txt),
[configuration](CONFIG), [status log](status.log), and
[lifecycle events](lifecycle-events.jsonl) retain the measured output and
execution record.

An earlier ad hoc invocation on this Image accidentally selected an obsolete
runtime archive without the third adaptation. Its audit rejected `netstat -i`
header errors despite observing 109 groups. The passing result above uses the
current three-adaptation archive; that earlier failure was a package-selection
error, not evidence of a kernel regression.

This is the compact native ALL profile: 8 MiB, one copy, FASTMEM, local
networking, no raw disks or remote hosts. It is a QEMU TCG compatibility
result, not a development-board performance score or proof that every LMBench
executable is covered. The full run is an on-demand milestone check; focused
QEMU regressions remain the inexpensive daily gate.
