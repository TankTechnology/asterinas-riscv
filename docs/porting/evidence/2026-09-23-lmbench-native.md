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
7. The host QEMU harness sent a serial log-read command every two seconds
   while LMBench ran. Focused active/quiet controls found RPC/UDP timeouts when
   those commands overlapped the benchmark and successful original-binary runs
   when collection was deferred. Full-suite validation therefore keeps the
   management console idle during signal and local-network measurements.

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

## Earlier unmodified binary QEMU result with active console polling

An earlier run booted RISC-V `virt` under TCG with four CPUs and 2 GiB RAM.
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
Thus the one-command workflow was operational and gave a precise incomplete
result under that test setup. The extracted `report.json`,
configuration, native result text, logs, QEMU summary and regression results
are stored in the adjacent `2026-09-23-lmbench-native/` directory.

## RPC/UDP isolation

`rpcinfo` showed the LMBench program registered for both UDP and TCP, and direct
UDP and TCP service probes succeeded. A small libtirpc client using the same XDR
character operation, 25 ms timeout and original LMBench server completed 10,000
calls in 3.84--4.20 seconds. Adding a fork and zero-timeout `select` between
calls also completed 10,000 calls. The original `benchmp` client instead failed
around call 5,883 while the instrumented server received more than 16,000
requests, showing frequent retransmission. Increasing its per-call timeout
tenfold did not resolve the failure. A later longer simple-client run also
failed; the defect is not exclusive to `benchmp`.

The same pinned source completed RPC/UDP and RPC/TCP on x86-64 Linux. The
remaining gap is therefore localized to the RISC-V Asterinas interaction with
high-volume RPC/UDP under QEMU TCG. It is not evidence for a general rpcbind
startup failure, and this change does not hide it.

### Bounded retry-interval experiment

The initial attempt to repeat this experiment accidentally used an older
browser kernel from the QEMU boot plan. Its RPC server could not bind an
anonymous UDP port, so those attempts say nothing about the LMBench failure.
Subsequent runs explicitly pinned the same kernel and Debian root image as the
native ALL run (SHA256 values above). RPC registration and direct UDP/TCP
service probes passed before each client trial.

With `ENOUGH=10000`, the unmodified `lat_rpc -P 1 -p udp localhost` timed out
in three consecutive trials after 14.061, 11.167 and 15.890 seconds. The
process returned status zero while printing `localhost: RPC: Timed out`, which
reinforces why the independent result auditor is necessary. In a separate boot
with the same kernel, a diagnostic build changed only
`CLSET_RETRY_TIMEOUT` from 2.5 ms to 25 ms. The same default-repetition
command completed in 26.096 seconds and printed an RPC/UDP latency result.
Both experiments used the original RPC server binary. A one-repetition
(`-N 1`) command also passed with the unmodified client; the failure appears
with the longer native sampling schedule.

The structured [retry experiment summary](2026-09-23-lmbench-native/rpc-udp-retry.json)
records the commands, artifact hashes, boot identities and observed outputs.
This supports excessive retransmission as a specific hypothesis, not a proven
kernel defect or an acceptable modification to the official benchmark. The
next diagnostic counted socket delivery and queue occupancy under the unchanged
native command. Native ALL remains **108/109**, failing.

### UDP queue and delivery accounting

A temporary diagnostic kernel on branch `codex/udp-rpc-instrument` at
`14ec53a87` counted nonempty UDP datagrams accepted or rejected by each socket
and printed final per-socket totals. It did not change the native LMBench
client or server. On a QEMU run that reproduced the original timeout, the RPC
client socket received 41,295 packets and dispatched 41,304; the server socket
received and dispatched 41,305. Neither socket reported a full RX queue.
The highest observed RX payload occupancy was 704 bytes on the server and 168
bytes on the client; the client queue still held 168 bytes when it closed. A
second run also timed out with roughly 55,000 accepted packets in each direction and zero
RX queue rejections. The [structured queue evidence](2026-09-23-lmbench-native/rpc-udp-queue.json)
records the kernel and boot identities, command, output and counters.

These are socket-level counts, not proof that each response matched its RPC
transaction ID. They rule out the proposed full receive queue in the observed
runs. At failure, the client still had 168 bytes queued, consistent with some
replies remaining unread when its 25 ms deadline expired. The next
discriminating test is to trace transaction IDs and client poll wakeups;
changing buffer sizes without that evidence would not address the observed
failure.

### Longer simple-client control

The previously successful simple libtirpc client was run for 50,000 calls
against the original LMBench RPC server, with a fork, zero-timeout `select`
between calls, the original 2.5 ms retry interval and the 25 ms total timeout.
It failed after 8,210 successful calls in 5.414 seconds. The client RX socket
still held 28 bytes at close; both sockets again reported zero RX queue
rejections. [The structured long-client result](2026-09-23-lmbench-native/rpc-udp-long-client.json)
records the counts and artifact identity. This overturns the earlier inference
that `benchmp` itself was required to trigger the failure. Duration or repeated
calls under these short RPC deadlines suffice; the exact mechanism remains
unproven.

### RPC transaction IDs and retry interval

The Nix closure includes libtirpc 1.3.6. Its `clnt_dg_call` waits with `poll`,
reads one datagram with `recvfrom`, and, on a mismatched reply transaction ID,
deducts the requested poll interval from the 25 ms budget before retransmitting.
This matters once short retries create stale replies. The source archive SHA256
and exact observations are in the [XID and retry evidence](2026-09-23-lmbench-native/rpc-udp-xid-retry.json).

A second temporary diagnostic kernel recorded the last RPC transaction IDs at
UDP send, arrival and read. In a failed 50,000-call simple-client trial, the
client completed 1,280 calls and closed with its last sent XID one ahead of its
last received and read XID. The server socket had already received and queued
that final request but had not read it. Neither socket reported an RX queue
rejection. The original 2.5 ms interval therefore allowed requests to get
ahead of the server in this run; the counters do not say why the server lagged.

With the unchanged LMBench server and a 25 ms total deadline, a 20,000-call
simple client failed at call 10,521 using 2.5 ms retries. In the same boot it
completed all 20,000 with 25 ms retries, one send and one receive per call.
In another boot, 5, 10 and 25 ms retries each completed 20,000 simple calls
without retransmission. Forty empty-pipe polls requested at 2 ms actually
waited 2.0515--2.1055 ms, so this small control did not reproduce an
immediately expiring poll timer. The full native-parameter `lat_rpc` client
still timed out with a 5 ms retry interval after 24.042 seconds. A passed short
client cannot substitute for the native benchmark's sampling schedule.

The 25 ms `lat_rpc` binary remains a clearly labeled diagnostic variant. It
must not be reported as an unmodified official result. A one-command native
ALL run with that single binary substitution produced the RPC/UDP row, but
RPC/TCP timed out in that boot. The independent auditor again found **108/109**
groups and rejected the run. The [diagnostic native report](2026-09-23-lmbench-native/rpc-retry25-native-all-report.json)
records the boot, kernel and substituted binary hashes. RPC/TCP succeeded with
the unmodified 25 ms total deadline in a later focused boot, showing that this
failure is intermittent. A 250 ms total-deadline diagnostic also completed in
that focused boot. These paired successes do not establish which part of TCP
handling caused the earlier long-tail timeout.

A combined diagnostic binary increased the RPC total deadline from 25 to
250 ms and set the UDP retry interval to the same value. It completed both
native-parameter `lat_rpc -P 1 -p udp localhost` and
`lat_rpc -P 1 -p tcp localhost` in one focused QEMU boot. This remains a
diagnostic benchmark adaptation, not a kernel fix or a claim that the
unmodified upstream suite passes. The [TCP and combined controls](2026-09-23-lmbench-native/rpc-tcp-timeout-controls.json)
record exact outputs and artifact identities.

### Linux RISC-V full-system control

To separate an Asterinas-specific failure from a general RISC-V binary or
QEMU TCG limitation, the **unmodified** `lat_rpc` binary and its packaged
libtirpc/rpcbind closure were run under a Linux RISC-V guest. This used the
same frozen Debian root filesystem as the Asterinas experiment, copied to a
private disk, and a minimal Ubuntu 5.15.0-1028-generic boot environment. QEMU
10.2.1 used `virt`, four virtual CPUs, 2 GiB RAM and TCG; unlike the Asterinas
boot, it loaded the Linux kernel directly with a minimal initramfs. The client
command and `ENOUGH=10000` matched the focused Asterinas trials. RPC program
404040 was registered over both UDP and TCP before each client run.

Two full-system Linux runs completed the original
`lat_rpc -P 1 -p udp localhost` command with measurements of 161.4145 and
161.3454 microseconds, after 27 and 31 seconds respectively. Neither printed
an RPC error. In contrast, the same original client timed out in three focused
Asterinas QEMU trials (14.061, 11.167 and 15.890 seconds), and the original
native ALL run omitted the RPC/UDP row. Three additional Linux user-mode
emulator trials also completed, but they bypass the guest kernel and are a
weaker comparison. The [structured Linux control](2026-09-23-lmbench-native/rpc-udp-linux-control.json)
records commands, package and binary hashes, output values and retained log
hashes. Raw local logs remain under
`target/lmbench-native-20260923/linux-user-oracle/`.

This control rules out a failure inherent to the pinned RISC-V binary or to
full-system TCG alone. It does **not** isolate the Asterinas network stack:
kernel scheduling, loopback delivery and RPC wakeups remain possible causes.
Cross-kernel latency values are not treated as a performance comparison because
the boot environments differ.

### Network poll-thread priority control

The Asterinas network-interface polling thread normally runs at fair-scheduler
`Nice::MIN`. In a temporary diagnostic kernel containing the existing UDP/XID
counters, its policy alone was changed to `Nice::default()`. The release kernel
booted the same Debian fixture and ran the unmodified RPC server and client.
The client still printed `localhost: RPC: Timed out` after 12.012 seconds.
Neither RPC UDP socket reported an RX queue drop, and the client closed with
168 bytes of replies queued. The [priority-control result](2026-09-23-lmbench-native/rpc-udp-poll-priority.json)
records the changed line, kernel hash, boot ID, raw-log hash and socket counts.
The temporary source edit was restored after this experiment. Lowering this
thread's priority alone is insufficient to recover the original benchmark;
it does not exclude other scheduler or socket-wakeup delays.

### RPC stage timing and retransmissions

The diagnostic kernel then matched RPC transaction IDs in a bounded per-socket
ring and sampled four stages at 1 ms timer resolution: request dispatch to
reply arrival, request arrival to server read, server read to reply dispatch,
and reply arrival to client read. The source is on the separate
`codex/udp-rpc-instrument` diagnostic branch at `99a3b78db`; none of its
counters are included in the production PR kernel.

The unmodified RPC/UDP command still timed out in two focused boots. In the
first, the longest observed UDP queue-to-read delay was 8 ms at the server and
1 ms at the client, with no delay reaching 25 ms. In the second, the client
recorded 10,530 retransmissions with the same transaction ID, while the
longest matched first-request-dispatch-to-reply-arrival sample was 11 ms.
Server queue-to-read peaked at 9 ms, server read-to-reply-dispatch at 7 ms,
and client queue-to-read at 3 ms. Neither socket dropped an RX packet. At
client close, 196 bytes of replies remained queued; the last read ID lagged
the last sent ID by one. The [stage-timing result](2026-09-23-lmbench-native/rpc-udp-stage-timing.json)
records both kernel hashes, boot IDs, command outputs and raw-log hashes.

This rules out a single observed 25 ms UDP queue wait as the necessary trigger
in those failures. It does not prove all RPC calls finished inside 25 ms:
the counters sample matching IDs in a bounded ring and the instrumentation
itself adds work. The pinned libtirpc 1.3.6 `clnt_dg_call` also deducts its
requested poll interval from the total budget when it reads a reply with a
different transaction ID, even if that poll returned early. Repeated short
retries and stale replies can therefore consume the 25 ms budget without a
single 25 ms kernel delay. A trace of the failing call's exact reply sequence
was needed to establish that as the final mechanism.

### Final failing RPC call

A bounded `LD_PRELOAD` diagnostic recorded only the original client's
`sendto`, `poll` and `recvfrom` calls on the **production** kernel. It left the
RPC server and benchmark binary unchanged. The unmodified command again
printed `localhost: RPC: Timed out` after 12.218 seconds. The worker retained
its last 256 syscall events; the [raw trace](2026-09-23-lmbench-native/rpc-udp-final-call-trace.log)
and [structured summary](2026-09-23-lmbench-native/rpc-udp-final-call.json)
are checked in. The tracer and summarizer source are on the separate
`codex/udp-rpc-instrument` branch at `e8fbc3517`.

For the final call, the client first sent transaction ID `1790897517` at
trace event 179618. It sent that same request 13 times. Of 13 polls, three
expired and ten returned readable; all ten reads yielded the *previous*
transaction ID `1790897516`, never the current one. The poll timeouts charged
6 ms against libtirpc's 25 ms budget, and the ten mismatched replies charged
another 19 ms. Those nominal deductions exhausted the budget, although only
9.1119 ms of wall time elapsed between the first send and last read. The
library then reported `RPC_TIMEDOUT`. This directly establishes the
stale-reply/retransmission feedback mechanism for this traced failure; it
does not establish why Asterinas QEMU accumulates the initial delayed replies
while the Linux full-system control completes. Interposition adds overhead,
so the trace is causal evidence for the failing call, not performance data.

### Intermittent original-client outcome

Later focused runs on the **same production kernel and original `lat_rpc`**
completed with measured UDP/RPC latencies of 365.9399, 378.9856 and 372.8906
microseconds. The last two calls ran consecutively in one QEMU boot. These
passes revise the earlier three-failure observation: the original command is
intermittent, not deterministically broken. A successful focused call alone
does not qualify native ALL.

The separate diagnostic kernel at `codex/udp-rpc-instrument` commit
`4cbdf6af1` counted short delays at four UDP stages. In one boot, the client
sent 44,224 RPC packets and recorded 21,809 retransmissions; its socket closed
while serial diagnostic output interrupted the harness control marker, so that
run has no trustworthy benchmark result. In another boot, the same original
command completed in 26.757 seconds with 68,756 client packets and **zero**
retransmissions. Both runs recorded no send-queue delay of at least 2 ms. The
retransmission-heavy boot did record more server receive-to-read delays of at
least 2 ms, but millisecond counters and instrumentation cannot identify the
initial cause. The [structured intermittent-run evidence](2026-09-23-lmbench-native/rpc-udp-intermittency.json)
retains the command, artifact hashes, boot identities, client output and raw
counter lines.

### Repeat unmodified native ALL qualification

After the three successful focused calls, the production kernel and original
runtime archive were run through `cd /opt/lmbench/src && make results` again in
QEMU boot `9b0e5ecf-63ea-4c5b-a7ff-44aeb4dd11ff`. The native command ran for
591.720 seconds. Its independent audit again found **108/109** measurement
groups: RPC/UDP was missing and the raw output contained
`localhost: RPC: Timed out`. The native driver itself returned zero, but the
supervisor rejected the incomplete result and returned status 2. This second
full run confirms that the focused successes do not make the unmodified ALL
suite reliable. The [audit report](2026-09-23-lmbench-native/rpc-original-second-native-all-report.json),
[raw native result](2026-09-23-lmbench-native/rpc-original-second-native-all-results.txt)
and [QEMU summary](2026-09-23-lmbench-native/rpc-original-second-native-all-qemu.json)
preserve the outcome and input hashes.

### First retransmission in a failing focused run

A bounded client-side interposition run captured the first repeated RPC request
instead of only the final timeout. In QEMU boot
`f4e62537-62ce-45c5-8024-d0d295e89caf`, the first original focused call
measured 438.1845 microseconds; a second call in the same boot printed
`localhost: RPC: Timed out` after 7.454 seconds. The original `lat_rpc` binary
and production kernel were unchanged. The trace library built from diagnostic
branch commit `38279a98d` retained the retry plus 63 preceding and 64
following events.

Transaction `1790023225` was sent at event 38193. The following `poll`
requested 2 ms and returned timeout after 2.078 ms; the client then resent
the same transaction at event 38195. It read the matching reply 4.0673 ms
after the initial send. The next transaction also crossed a 2 ms poll timeout
and then read a reply for `1790023225`, starting the stale-reply sequence.
At the final failed call, the same run showed 13 sends, ten stale replies and
three poll timeouts; libtirpc deducted its full 25 ms budget in 9.0028 ms of
wall time. The [first-retry summary](2026-09-23-lmbench-native/rpc-udp-first-retry.json),
[first-retry trace](2026-09-23-lmbench-native/rpc-udp-first-retry-trace.log)
and [final-call trace](2026-09-23-lmbench-native/rpc-udp-first-retry-final-call.log)
preserve the evidence.

This establishes that a real short poll timeout preceded the first retry in
this traced run. The interposition adds overhead, and the client trace cannot
attribute the initial response delay to a kernel, server or QEMU stage. It
does not prove that every uninstrumented failure has the same first trigger.

### Wakeup-stage timing control

An isolated diagnostic kernel from `codex/udp-rpc-instrument` at `301071ab8`
recorded nanosecond timestamps for each stage of one RPC transaction and
retained the slowest matching request in a focused run. In a successful run
with zero retransmissions, that request took 1.4396
ms from first dispatch to client read. The server finished its readable-event
notification **1,136.5 microseconds before** the server process read the
request; the notification call itself took 51.0 microseconds. Request dispatch
to server receive and reply dispatch to client receive each took under 5
microseconds in this sample. Thus the observed slow tail lay mainly after
readiness notification, not in packet transfer.

A separate diagnostic control made the network polling thread voluntarily
yield after each `iface.poll()`. Its one successful run also had zero
retransmissions. The slowest request then spent 378.2 microseconds between
server notification and read, but 1,064.8 microseconds between client
notification and read; its overall 1.7548 ms round trip was longer. These are
single maxima from different runs, so they do not prove that yielding helps or
hurts. A second control combined the yield with default nice priority for the
polling thread. Its slowest request spent 129.8 microseconds after server
notification but 1,044.8 microseconds after client notification, for a 1.3888
ms round trip. Both controls were reverted and are **not** in the production
kernel. The [stage-level evidence](2026-09-23-lmbench-native/rpc-udp-wakeup-stage.json)
records each event, timing, command, boot identity and kernel hash.

The code path is `UdpSocketBg::process` → `DatagramObserver::on_events` →
`Pollee::notify` → `Waker::wake_up` → scheduler enqueue. The background poll
thread uses `Fair(Nice::MIN)`; a default-nice server task need not preempt it
on the same CPU. This is a concrete scheduling hypothesis consistent with the
successful-run tail, not yet a verified explanation for the original ALL
failures. A failing run with the same per-transaction kernel trace is reported
below; it still does not justify changing production scheduling behavior.

A further focused run counted successful first notifications delivered by
`Waker::wake_up` while each socket notification call ran. The slowest request
had one such notification during the server call (25.7 microseconds); the
server read followed 1,155.9 microseconds later. The client call also
overlapped one successful notification, and its read followed 125.3
microseconds later. This run passed with zero retransmissions. The count is
global, so unrelated concurrent wakeups cannot be excluded. A successful
`Waker` call also does not prove that the target task was already parked.
The [notification evidence](2026-09-23-lmbench-native/rpc-udp-waker-stage.json)
retains the XID, stages, boot identity, kernel hash, and these limits.

The same diagnostic kernel then ran the original package through the native
`make results` driver. It captured the **first** non-rpcbind RPC/UDP retry:
the original request reached the server within microseconds, but the server
did not read it until **2,687.6 microseconds after** its first readiness
notification ended. The client retried after **2,157.2 microseconds**, before
that read. The first matching reply was read by the client 3,059.8
microseconds after the original send. The global successful-notification
counter increased by one during the first server notification. The
[first-retry kernel stages](2026-09-23-lmbench-native/rpc-udp-first-retry-kernel-stage.json)
preserve the XID and all 23 recorded stages. This is a concrete delay at the
server side of the kernel-to-user boundary; it does not yet distinguish task
scheduling from work done by the server before its socket read.

The diagnostic native ALL run finished in 582.828 seconds. Its native driver
returned zero, but the independent auditor rejected it: **107/109** expected
groups were present. RPC/UDP again emitted `localhost: RPC: Timed out`; the
protection-fault signal measurement was also absent, with no accompanying
error line. The latter measurement was present in both earlier unmodified
ALL runs and the adapted run, so this single omission is an additional
intermittent observation, not evidence of a UDP-related defect. The
[diagnostic report](2026-09-23-lmbench-native/rpc-diagnostic-original-all-report.json)
and [raw native result](2026-09-23-lmbench-native/rpc-diagnostic-original-all-results.txt)
retain the complete audit. At that point, this run ruled out an unconditional
109/109 pass claim under the active-console test setup; the quiet-console
controls below revised that assessment.

### Short native-network reproducer

Running `lat_rpc` alone is a weak discriminator: one diagnostic-kernel call
passed in 26.530 seconds with zero retransmissions. A shorter reproducer now
follows the native script's local-network order: start `rpcbind`, `lat_rpc`,
`lat_connect` and `bw_tcp` servers; start, measure and stop `lat_udp`; do the
same for `lat_tcp`; then run the original `ENOUGH=10000 lat_rpc -P 1 -p udp
localhost`. It omits `lmhttp` and non-network benchmarks, so it does not claim
to be equivalent to ALL. It completes in roughly one minute.

With host serial log-read commands starting 35 seconds after guest launch,
the original RPC client timed out in this sequence on both the diagnostic
kernel and the uninstrumented production kernel. The diagnostic run's first
retry occurred 2,216.4 microseconds after initial enqueue; the server's first
notification ended **6,065.2 microseconds before** its first request read.
The client recorded 6,691 retransmissions before exit. Two single-run
poll-thread controls—yielding at `Nice::MIN`, and yielding at default nice—
also timed out while the host was polling serial. Neither was retained.
The [short-sequence evidence](2026-09-23-lmbench-native/rpc-udp-network-sequence.json)
includes the commands, boot and kernel hashes, outcomes, and first-retry stages.

Reducing the guest workload initially appeared to implicate the preceding
network tests: UDP-only, TCP-only, and even a 26-second idle wait before RPC
each had a timeout. The runners revealed a confounder. They began sending a
guest command over serial every two seconds after a 35-second startup delay;
these commands overlapped the RPC trial. On the **same production kernel**,
delaying host serial collection to 85 seconds gave a successful RPC result
after both the 26-second idle wait and the native network sequence. Each A/B
case is one boot, so this supports serial management activity as a material
timing perturbation, not a guarantee that it is the only possible cause.
The [serial-interference controls](2026-09-23-lmbench-native/rpc-udp-serial-interference.json)
record the guest workloads, host collection timing, and four results.

### Unmodified native ALL with quiet console

The original `lat_rpc` SHA256
`baad9f0281f2e948eda0f950acc2fd8506530ccbbf676167cc4a7c19c0dd3908`
was then run through `cd /opt/lmbench/src && make results` in boot
`ac873e67-0031-4fe4-b86d-d6174a761691`. The production kernel SHA256 was
`1d59c9ebb44389f3b7d3473bb6db6c8eae5c39f8d28f76336fd4639610284cb0`.
The host deferred serial log-read commands for the first 180 seconds, covering
the signal and local-network phases. The native driver returned zero after
597.156 seconds; independent auditing found **109/109** expected groups, no
missing measurements and no error lines. The raw result contains the
protection-fault signal measurement and RPC/UDP and RPC/TCP latencies of
370.6451 and 751.8556 microseconds. See the
[original quiet-run report](2026-09-23-lmbench-native/rpc-original-quiet-native-all-report.json),
[raw result](2026-09-23-lmbench-native/rpc-original-quiet-native-all-results.txt),
and [configuration](2026-09-23-lmbench-native/rpc-original-quiet-native-all-config.txt).
This is one successful full run under a controlled console; it does not
establish reliability under arbitrary concurrent management activity.

### Final package qualification with original binaries

The final runtime was rebuilt from the pinned fork without the earlier RPC
timing patch. Its archive SHA256 is
`7989ad15079a55acb2e337bbc0a4fd663f973e8ce0f228e5b8626a57777f6628`;
the packaged `lat_rpc` SHA256 is the original
`baad9f0281f2e948eda0f950acc2fd8506530ccbbf676167cc4a7c19c0dd3908`.
Runtime metadata records no modified benchmark binaries, the upstream 25 ms
RPC deadline, and the 2.5 ms UDP retry interval. The preceding quiet-console
run used an earlier runtime archive, so the final archive was tested again.

In QEMU boot `8c2af2f8-01ad-4c0e-bb62-dd48fc178ecb`, with production kernel
SHA256
`1d59c9ebb44389f3b7d3473bb6db6c8eae5c39f8d28f76336fd4639610284cb0`,
the guest ran `cd /opt/lmbench/src && make results`. Host serial log-read
commands were deferred for the first 180 seconds. The native driver returned
zero after 595.977 seconds; independent auditing passed **109/109** expected
measurement groups with no missing results or errors. The result includes
protection-fault signal latency at 12.3792 microseconds, RPC/UDP at 378.2335
microseconds, and RPC/TCP at 761.4541 microseconds. The
[final audit report](2026-09-23-lmbench-native/rpc-final-original-quiet-native-all-report.json),
[raw native result](2026-09-23-lmbench-native/rpc-final-original-quiet-native-all-results.txt),
and [configuration](2026-09-23-lmbench-native/rpc-final-original-quiet-native-all-config.txt)
are preserved. The package and smoke unit suite passed 28 tests, including
both `make results` and the `make result` alias; `git diff --check` passed.
These two full quiet-console passes establish the selected local ALL suite on
this QEMU setup, not a guarantee under concurrent serial management load or
on the physical board.

### Adapted native ALL qualification

The combined diagnostic `lat_rpc` binary was substituted into an otherwise
unchanged native runtime archive. In QEMU boot
`0637562f-1411-441e-93b2-7ae8fb6d5af8`, using production kernel SHA256
`1d59c9ebb44389f3b7d3473bb6db6c8eae5c39f8d28f76336fd4639610284cb0`,
the guest ran `cd /opt/lmbench/src && make results`. The native driver returned
zero after 625.576 seconds. Independent auditing found all **109/109** expected
groups, zero missing measurements and no error lines. The raw result includes
RPC/UDP at 386.9503 microseconds and RPC/TCP at 783.7662 microseconds. See the
[adapted native report](2026-09-23-lmbench-native/rpc-adapted-native-all-report.json).

This earlier control demonstrated that the pinned fork's native scripts and
selected ALL suite could complete with an adapted RPC timing budget. It was
not evidence for the original binary. The subsequent quiet-console run above
passed with the original binary, so the native package no longer needs this
timing adaptation. The control report remains to explain the diagnostic path.

## Earlier adapted-package verification

The combined native-runner and daily-smoke unit suite passed all 28 tests. Rust,
C, Nix and Python format/syntax checks passed, as did `git diff --check`. A clean
RISC-V release kernel build with four CPUs and `riscv_sv39_mode` completed in the
persistent development container. The final kernel source differs from the QEMU
kernel only by removing temporary RPC diagnostics; the tested UDP source
selection and procfs implementations are unchanged.

For the adapted native package, the Nix build produced `lat_rpc` SHA256
`1095906004c6532897aa72403d5d45b5fcb204dd395e57affff6e58367d690cd`,
identical to the binary used in the passing 109/109 QEMU run. The ordinary
benchmark package retained its upstream `lat_rpc` SHA256
`baad9f0281f2e948eda0f950acc2fd8506530ccbbf676167cc4a7c19c0dd3908`.
The production archive contains the same 283 other regular `/opt/lmbench`
files as the passing diagnostic archive; only runtime metadata and the bundled
supervisor differ. The latter difference is timeout-input validation added
after the diagnostic archive was created. The combined native-runner and smoke
unit suite passed 28 tests again, and the adapted Nix package built cleanly.
The earlier adapted runtime archive SHA256 is
`19066539ed4a52d3deb8789a70df35d9bfc82854f38f3010e3eeb9d78dd35e82`.
