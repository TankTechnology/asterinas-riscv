# Native LMBench ALL on the current network stack

The RISC-V QEMU kernel from `codex/netns-poll-lifecycle` commit
`a649e5c19595fb46c27a3bfb0e002ccd619841a1` was built with
`make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1`.
The immutable kernel image used for this run has SHA256
`c4c22a9e6c51247eb20f266a58791089b704cf2f981566201dd99dc3041d216c`.
The QEMU gate's [summary](qemu-summary.json) confirms that exact input, Debian
13.7, a nonce-framed local root debug console, `physical=false`, and an
overall pass. The guest boot ID was `7dc0540d-70c2-4ae4-94b3-8fe4892de011`.

The boot serial also reported a failure of
`asterinas-desktop-ready.service`. Its `startup-ready` command requires an
armed software-reboot watchdog, while this QEMU command line has no
`asterinas.reboot_after` parameter. The debug-console gate separately
observed active graphical and desktop services and the benchmark ran to
completion. This run does not qualify the physical-board readiness unit.

The guest ran `cd /opt/lmbench/src && make results` using the pinned
`asterinas/lmbench` revision `afb47eddaf10a411c1ea3cb64965461f1308a6ea`.
The packaged runtime SHA256 was
`e65c52261a1483c12196840a7d15f70ea272bad7d0dc66ad3d10b72552961087`.
Its GNUmakefile delegates to the original Makefile with `-o lmbench`, skipping
in-guest compilation. The original benchmark binaries remain unchanged; for
example, `lat_rpc` SHA256 was
`baad9f0281f2e948eda0f950acc2fd8506530ccbbf676167cc4a7c19c0dd3908`.
The declared script adaptations are explicit IPv4 loopback server arguments,
`grep -E` in place of the `egrep` wrapper, and skipping modern `netstat -i`
column headings. The guest left the serial control channel quiet for 180 seconds
after launching the suite so polling would not perturb local network timings.

The native driver and outer QEMU gate both exited zero. Independent result
auditing found **109/109 selected measurement groups**, no missing groups, no
errors, and no metadata warnings; the native suite took 592.881 seconds. The
[report](report.json), [raw result](native-results.txt), [configuration](CONFIG),
[status log](status.log), [lifecycle events](lifecycle-events.jsonl), and
[host runner log](runner-host.log) retain the underlying evidence. Their SHA256
values, in that order, are:

- `c3c685c99bcd286deeecb6497bd8462dd999c84f65a56c3f9c8e75930be5da37`
- `7e160e4d7640ecc836e0ccbfce0ac168df2f177e2d987b1fd35487d362a7df85`
- `b49c7d45f0904ca7217cd7109efadb8f63d687e7dff1e7f2e60c17c06f3d5599`
- `6d1a16c9160fa858a871eaa2f4ed41dd840fd6936a1e9c29e440f0de28bf50bd`
- `b57f438f617263f4a94679aff2a0880d9db56f0df5c82037b0027b4db72e0f45`
- `779707056ae0d8ad6a1a081ec6a85b2fd581d7e51b671ac58a8871121eb53487`

The selected configuration was native ALL, 8 MiB, one copy, FASTMEM, with no
raw disk or remote host. This is a QEMU TCG compatibility result, not a
hardware performance score or exhaustive coverage of every LMBench executable.
It qualifies the current PR stack; it does not imply these draft changes are
already in remote `main`.
