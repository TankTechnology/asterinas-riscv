# LMBench and ICMP integration check

Integration commit `4aca2192992e1296889858f3c08aca39d99000ce` merges the
review-ready native LMBench branch (`a8f2e445f35694c102a0a776d20803237eec3313`)
with the physical-board ICMP branch (`4e51337349101c23155c9bbb1448c8775b9d67bc`).
The merge had no conflicts and the worktree was clean after testing.

The persistent project container built this exact combined source with
`make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode`. The resulting
development Image SHA-256 is
`a8423fca2a66ca09b97bce60e7d05ede211e0733f8439fafad3dfb34f6a391be`.
The [QEMU kernel-test log](bigtcp-ktest-qemu-serial.log.gz) records all **21/21**
`aster-bigtcp` tests passing, including both ICMP Echo tests. The
[native-runner unit log](lmbench-native-unit.log) records **23/23** passing.

The actual packaged runtime archive was rechecked with
`python3 tools/riscv/lmbench_native.py verify --archive`. Its SHA-256 remains
`54bf3c45ca425b8736e54e9723ff50d6c4c306af26ff033c25f89c4272236af3`.
With both packaged `GNUmakefile` and original `Makefile` extracted into one
temporary source directory, `make -n result` and `make -n results` each expanded
to `python3 ../asterinas-native.py run` ([output](make-entry-check.txt)). This
confirms the leader-requested singular command selects the same runner without
repeating the full suite.

The full native LMBench ALL result on the LMBench parent is recorded in
[the original qualification](../2026-09-24-native-lmbench-no-egrep/README.md),
and the ICMP candidate's Ethernet behavior was verified on Megrez in
[the board qualification](../2026-09-24-icmp-echo/README.md). This integration
check confirms the two changes build and pass their focused tests together;
the later native ALL result on this combined release Image is recorded in
[the final qualification](../2026-09-24-native-lmbench-integrated-release/README.md).
Neither result is a board LMBench performance score.

## Additional 18-case smoke attempt

The older 18-case QEMU smoke driver was replayed against the combined Image
`a8423fca2a66ca09b97bce60e7d05ede211e0733f8439fafad3dfb34f6a391be`.
Its Debian root, Stage1, DTB, U-Boot, and Nix-built LMBench bundle matched
the hashes in the original successful smoke run.
The first four replays **did not reach the benchmark cases** and are not
passing integration results.
They booted Debian and obtained a UID-0 debug console, but the
older desktop setup reported a readiness failure and bounded `daemon-reload`
timeouts.

| Attempt | First blocking result |
| --- | --- |
| `integration-smoke` | The old guest driver timed out while extracting the 15 MB bundle after 20 seconds. |
| `integration-smoke-retry` | The serial-transferred Python source contained a NUL byte and would not parse. |
| `integration-staged` | A malformed scratch SHA-256 comparison command rejected the correctly staged source before execution. |
| `integration-staged-final` | The guest verified the staged source hash and extracted the bundle in 63.682 seconds, then timed out stopping `asterinas-desktop-m5.service` after six seconds. |

The [four QEMU result files and serial captures](smoke-attempts/) preserve
the failures and input identities.
In the staged attempt, an offline `debugfs` readback matched the generated
source SHA-256 `a374abd9ae451877395a1b122f7bf2f9785d9f68e61c20fc987596df21c71783`.
The last attempt verified the same hash inside the guest.
Only scratch copies of the historical driver were changed to use a 90-second
extraction bound and stage the script in the disposable root image;
the tracked runner, kernel, benchmark bundle, and original root image were not
changed.
The parent native ALL result and combined focused tests above remained the
qualification evidence at that point.

## Console-only replay and release qualification

The historical smoke driver also waited for graphics readiness and tried to
stop the desktop service before measuring.
For this compatibility check, a scratch copy instead waited for the root
console marker, proved UID 0 and the boot ID with nonce-framed commands,
verified the staged script SHA-256, and ran the same tracked 18-case guest
runner without stopping the desktop.
Its Nix-built binaries, workload arguments, timeouts, parser, and guest result
format were unchanged.
The final scratch [host driver](smoke-attempts/scratch-run.py) and
[guest driver](smoke-attempts/scratch-guest.py) are retained for replay;
they are evidence helpers rather than installed benchmark entry points.
This is a bounded compatibility check, not a performance comparison.

The combined **development** Image failed once while Python imported the
existing `threading` bytecode (`bad marshal data`).
Offline reads found the original and post-run bytecode files identical, and
the original root image passed read-only `e2fsck`.
The older shebang-fixed **release** Image
`78ec31caa7cad3db6cd5f896bd2f7776c45317d071793a7f564d9f14bb82cac9`
then passed the same console-only runner **18/18**.
A repeat on the combined development Image
`a8423fca2a66ca09b97bce60e7d05ede211e0733f8439fafad3dfb34f6a391be`
also passed **18/18**.
In both successful boots, the guest reported the expected `threading.pyc`
SHA-256 `2eeabd656bcba85331faeed7c3aabb87c7560fe2dc5d3756d59901c054e76622`.
The one-time import failure remains unexplained and is not evidence of a
repeatable regression.

The persistent project container built the exact integrated source with
`make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1`.
The resulting **release** Image SHA-256 is
`43ee67d0c18c88e2314e2867c624d47cdcfbb61555e8435b0a1e77946f8b74f7`.
Its QEMU gate exited zero; the [guest report](smoke-attempts/integration-release-smoke-report.json)
records **18/18** passing cases, no timeouts, and 9.104 seconds of guest
measurement time.
The [outer result](smoke-attempts/integration-release-smoke-result.json),
[serial capture](smoke-attempts/integration-release-smoke-serial.log.gz),
and [parent control](smoke-attempts/parent-console-control-report.json)
retain input identities and the earlier comparison.
The full native `make results` ALL run was subsequently completed on this
exact combined release Image, with 109/109 groups in
[the final qualification](../2026-09-24-native-lmbench-integrated-release/README.md).

After integrating the independent rustfmt and cross-architecture build fixes,
the new release Image passed a further [18/18 short QEMU replay](merged-baseline/README.md).
