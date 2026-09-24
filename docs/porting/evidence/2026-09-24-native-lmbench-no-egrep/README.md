# Native LMBench ALL with the upstream version probe

The pinned `asterinas/lmbench` revision
`afb47eddaf10a411c1ea3cb64965461f1308a6ea` ran its own
`cd /opt/lmbench/src && make results` command on a Debian RISC-V QEMU guest.
The GNUmakefile skips only compilation and delegates to the native Makefile.
The archive SHA-256 was
`54bf3c45ca425b8736e54e9723ff50d6c4c306af26ff033c25f89c4272236af3`;
its SHA-256 sidecar and `verify --archive` preflight passed. The archive's
`scripts/version` has the same SHA-256 as the pinned upstream source,
`ff4efc4ded0d0a0ec0214501906f7117ca681f1fd4aab25dd73580aff72ac841`.
The only two declared script adaptations give local network servers an
explicit IPv4 address and skip modern `netstat -i` table headers.

This guest used the shebang-fixed Image SHA-256
`78ec31caa7cad3db6cd5f896bd2f7776c45317d071793a7f564d9f14bb82cac9`.
The unchanged Debian `/usr/bin/egrep` shell wrapper needs the script pathname
preserved in interpreter arguments; the short old/new Image oracle for this
behavior is in the [shebang evidence](../2026-09-24-shebang-script-argv/README.md).

The native command exited zero. The [audit](report.json) found **109/109
measurement groups** in 571.427 seconds, with no missing groups, errors,
metadata warnings, or timeout. The [QEMU summary](qemu-summary.json) records
the exact input hashes, UID-0 debug console and passing outer gate. The
[raw results](native-results.txt), [configuration](CONFIG),
[status log](status.log), and [lifecycle events](lifecycle-events.jsonl)
retain the compact execution evidence.

This is the native ALL selection: one copy, 8 MiB, FASTMEM, local networking,
no raw disks or remote hosts. It does not select every standalone executable
in the repository. This is a QEMU TCG compatibility result, not a board
performance score. The full suite remains an on-demand milestone test; the
focused 18-case QEMU smoke is the inexpensive daily gate.
