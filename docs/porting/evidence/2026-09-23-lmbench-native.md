# Native LMBench results on RISC-V Asterinas

This work changes the acceptance target from the previous 18-case compatibility
smoke to the pinned **asterinas/lmbench native `make results` workflow**.
The package revision remains `afb47eddaf10a411c1ea3cb64965461f1308a6ea`.
The native target is plural `results`; the packaged entry also accepts `result`.
Compilation happens in the persistent development container, not inside Asterinas.

## Native coverage and cost

The configuration is native ALL, one copy, 8 MiB, FASTMEM, filesystem tests on,
loopback network tests on, raw disks/remote hosts unset, mailing disabled.
This includes syscalls, select, signals, processes, pipes/Unix sockets,
TCP/UDP/RPC/HTTP, filesystem/mmap/pagefault measurements, CPU operations,
context switches, memory bandwidth, TLB, memory parallelism, both STREAM
versions, and regular/random memory latency curves.

ALL is the set selected by the native driver, not every standalone executable:
`lat_fcntl`, `lat_fifo`, `lat_sem`, `lat_unix_connect`, `lat_usleep` and others
remain separate. The driver's cache-parameter command is commented out upstream.
Raw-disk and remote-machine tests require additional explicit configuration.
No claim about those configurations follows from this local run.

The 18-case smoke remains the cheap daily gate. Native ALL retains upstream
repetitions and command arguments and is an on-demand suite qualification,
with a 900-second failure-containment deadline. `ENOUGH=10000` limits calibration
for benchmarks honoring that override, but several native tests specify longer
sample intervals. Neither run is a physical-board performance measurement.

## Problems isolated

1. Unmodified `make results` first tries to compile. The packaged GNUmakefile
   delegates to the original Makefile with `-o lmbench`, preserving the native
   configuration and result-writing targets while skipping compilation.
2. This fork's `lat_udp`, `lat_tcp`, `lat_connect`, and `bw_tcp` require an
   address after `-s`; the native shell driver still omits it. A declared script
   adaptation supplies `127.0.0.1`, retaining `lat_rpc -s` unchanged.
3. The Debian guest lacks the `rpc` account expected by its cross-built rpcbind.
   Installation creates a locked system account with no home or login shell.
4. A PATH-launched shell script receives its caller's argv[0] as the script
   pathname. The `egrep` wrapper consequently fails while `/usr/bin/egrep`
   succeeds. The version probe uses `grep -E` as a declared compatibility
   adaptation. This does not fix the general execve/shebang issue.
5. libtirpc 1.3.6 requires `/proc/sys/net/ipv4/ip_local_reserved_ports` even when
   no ports are reserved. The kernel now exposes its actual empty reservation
   set read-only; writable reservation configuration is not implemented.
   Its five-check C regression fails with ENOENT on the old kernel and passes
   on the patched kernel.
6. A wildcard UDP socket on the Ethernet interface answers a loopback request
   using the Ethernet source address. The short baseline echo returned
   `10.0.2.15`; an explicit loopback bind returned `127.0.0.1`. Source selection
   now fills in a loopback source for wildcard IPv4 local delivery, preserving
   both explicit socket binds and per-packet source overrides.

Several failures were in the new test harness, not the kernel. Copying the raw
native driver over its built counterpart lost the build-time `<version>`
substitution; packaging now adapts both copies while preserving that substitution.
The TCP bandwidth rows include a third `MB/sec` field; HTTP can use either
`KB/sec` or `MB/sec`. The auditor handles the actual upstream formats.
An early serial observer repeatedly printed the entire growing log and hit its
command deadline; subsequent runs transfer only new bytes. That event is not
evidence of a kernel hang.

`rpcbind -h 127.0.0.1` adds a duplicate loopback bind with this rpcbind version,
producing an address-in-use diagnostic. The isolated guest uses `rpcbind -f`.
The test service is removed during cleanup and QEMU is torn down after the run.

## Reproduction and evidence

See [the reusable build/install/run instructions](../../../tools/riscv/README.md#native-lmbench-make-results).
The guest entry is `cd /opt/lmbench/src && make results` after one-time installation.
It runs the upstream config and results scripts, keeps original raw outputs,
and audits them independently because native scripts can return zero after
individual failures. Missing curves, errors, timeouts and artifact failures
produce a nonzero result. Each run retains its configuration, answers, logs,
raw output, binary hashes and structured report.

The input image is the frozen Debian browser fixture, SHA256
`bd855c9855e6734cb5e154d488d7c1899f9c1949ac01dae87cd0fc88702e35cf`.
QEMU uses RISC-V virt, TCG, four CPUs and 2 GiB RAM. The desktop is stopped for
benchmark execution. Each boot uses a private root-disk copy; no board was used.

## Final QEMU result

The final run booted RISC-V `virt` under TCG with four CPUs and 2 GiB RAM.
Its boot ID was `1ee319bd-b45f-414b-a357-f4982c5ce55c`. The kernel SHA256 was
`1d59c9ebb44389f3b7d3473bb6db6c8eae5c39f8d28f76336fd4639610284cb0`;
the packaged runtime SHA256 was
`2cc35686d2fd2415ba0629cbf2db9fe4a94622bc0b4e5bcb816ce8ed8539202e`.
Both new C regressions passed: all five reserved-port checks and both UDP
loopback-source cases.

After the run, the final source was packaged again as
`runtime-final-source.tar.gz`, SHA256
`48fda4df01e960ae1f1f2e79dd369b928d9fd87acb356fce2e1012487d7c691f`.
The only runner change after the QEMU bundle was validation that rejects a
non-positive or non-finite timeout before starting any process.

The native driver completed in 586.841 seconds and returned zero. Independent
auditing found 108 of 109 expected measurement groups, including RPC/TCP, and
rejected the run because RPC/UDP was missing with `localhost: RPC: Timed out`.
Thus the one-command workflow is operational and gives a precise incomplete
result, but native ALL is not yet fully passing. The extracted `report.json`,
configuration, native result text, logs, QEMU summary and regression results
are stored in the adjacent `2026-09-23-lmbench-native/` directory.

## RPC/UDP isolation

`rpcinfo` showed the LMBench program registered for both UDP and TCP, and direct
UDP and TCP service probes succeeded. A small libtirpc client using the same XDR
character operation, 25 ms timeout and original LMBench server completed 10,000
calls in 3.84--4.20 seconds. Adding a fork and zero-timeout `select` between
calls also completed 10,000 calls. The original `benchmp` client instead failed
around call 5,883 while the instrumented server received more than 16,000
requests, showing retransmission and missing replies in that specific client
path. Increasing its per-call timeout tenfold did not resolve the failure.

The same pinned source completed RPC/UDP and RPC/TCP on x86-64 Linux. The
remaining gap is therefore localized to the RISC-V Asterinas interaction with
LMBench's `benchmp` RPC/UDP measurement path. It is not evidence for a general
rpcbind, libtirpc or UDP loopback failure, and this change does not hide it.

## Final verification

The combined native-runner and daily-smoke unit suite passed all 28 tests. Rust,
C, Nix and Python format/syntax checks passed, as did `git diff --check`. A clean
RISC-V release kernel build with four CPUs and `riscv_sv39_mode` completed in the
persistent development container. The final kernel source differs from the QEMU
kernel only by removing temporary RPC diagnostics; the tested UDP source
selection and procfs implementations are unchanged.
