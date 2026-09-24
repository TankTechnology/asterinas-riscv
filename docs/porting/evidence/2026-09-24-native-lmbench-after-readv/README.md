# Native LMBench ALL on the combined socket receive branch

The pinned `asterinas/lmbench` revision
`afb47eddaf10a411c1ea3cb64965461f1308a6ea` ran its own
`cd /opt/lmbench/src && make results` entry point in Debian RISC-V QEMU.
The packaged `GNUmakefile` delegated to the original Makefile with
`-o lmbench` so that the prebuilt benchmark binaries were used without
compiling inside Asterinas. The archive SHA-256 was
`e65c52261a1483c12196840a7d15f70ea272bad7d0dc66ad3d10b72552961087`.

The [QEMU summary](qemu-summary.json) reports a passing gate,
`physical=false`, and kernel image SHA-256
`5e1c85734c79bd85680eb8895780552932f1446430968908032e536ed91c8703`.
That is the image produced from commit
`8d4495913ff9c78ff0e45fda0748db83fc8020b8`, after the UDP/TCP
prefault, deferred-fault, TCP copy/ring-wrap, and socket `readv` changes.

The native driver [report](report.json) records 109 of 109 selected
measurement groups, zero missing groups, errors, or metadata warnings,
`native_exit_status=0`, and 573.47 seconds elapsed. The
[raw results](native-results.txt), [configuration](CONFIG),
[status timeline](status.log), and [guest lifecycle](lifecycle-events.jsonl)
retain the measurements and execution evidence. The guest also passed
its reserved-ports and UDP-loopback regressions before starting LMBench.

This is the compact native ALL profile: 8 MiB, one copy, FASTMEM, local
networking, no raw disks or remote hosts. The measurements are QEMU
results and are not development-board performance scores. The full run
is intended as an on-demand milestone check; the focused QEMU regression
suite remains the inexpensive daily gate.
