# Firefox dmesg and command-boundary correlation

## Result

One bounded RISC-V QEMU run reproduced the Firefox failure with a complete
kernel-log frame and a full 300-second selected-command budget.  The first
missing observed boundary is before or at Firefox command dispatch:

```text
Marionette client request 4
  -> send complete (1176-byte command)
  -X-> Firefox TCPConnection.execute / server.command
      -> GeckoDriver.executeScript
      -> parent/content actor chain
      -> response queue
```

This identifies the first broken boundary, not the causal kernel line.  In
particular, the evidence does not yet prove a TCP lost packet or a missed
`ppoll` wakeup.  The run is QEMU diagnostic evidence only; it is not a
physical-board run or browser acceptance.

## Scope and fixed inputs

The experiment used the persistent development container offline.  It did not
install Cargo OSDK or any package, rebuild the kernel, delete an image or
container, update a remote PR, or change Firefox's page/command semantics.
It used QEMU virt, 2 GiB RAM, SMP=4, the existing browser-web profile, and a
disposable copy of the frozen root image.

The successful evidence run was invoked as:

```sh
tools/docker/run_dev_container.sh \
  --workspace /home/ubuntu/.config/superpowers/worktrees/asterinas/megrez-physical-graphics-current-main \
  --offline -- \
  python3 tools/riscv/diagnostics/firefox_dmesg_experiment.py \
    --kernel target/firefox-dmesg-20260908/kernel.Image \
    --uboot target/current-main-physical-graphics/inputs/u-boot \
    --dtb target/current-main-physical-graphics/inputs/qemu-virt.dtb \
    --stage1-initramfs target/debian-riscv/stage1/initramfs.cpio \
    --root-image target/firefox-dmesg-20260908/browser-rootfs-actor-v2/debian-root.ext2 \
    --root-manifest target/firefox-dmesg-20260908/browser-rootfs-actor-v2/rootfs-manifest.json \
    --packages-lock target/firefox-dmesg-20260908/browser-rootfs-actor-v2/packages.lock \
    --package-checksums target/firefox-dmesg-20260908/browser-rootfs-actor-v2/source-metadata/package-checksums \
    --output-directory target/firefox-dmesg-20260908/run-02 \
    --smp 4 --boot-timeout 600 --command-timeout 300 --cleanup-timeout 10
```

The boot arguments retain `loglevel=off` while adding
`asterinas.klog_capture=info asterinas.syscall_diag=1`.  Thus informational
records are retained for `dmesg` without flooding the serial console.
`ASTERINAS_FIREFOX_ACTOR_DIAGNOSTICS=1` enables the bounded scalar-only actor
markers in the derived Firefox archive.

| Input | SHA-256 |
| --- | --- |
| Kernel image | `c9ba30a445d5d66f559a33c51de809234707a7394a7efb99652cf82084e40f02` |
| Root image | `edb3252f289f16d166b2ae4bdd8e801e6bea90459451a25d70a47bfa166f89d8` |
| Root manifest | `f24b5007ddd42f2b0ee7cac4cc245b3ce68773da1ed768996a37adc03b28727f` |
| Package lock | `d4381641088a10429b26ac478e43e2d7b54ca26b9be0a144a5e6e1e418449c23` |
| Package checksums | `734835671bcca416329fd4f86bf471be8261cd798903131a0ca9c9e9d5c99f46` |
| U-Boot | `ff9abcc4d04ecae76645ddd8f42a61d314938a66f98e91878faedb2633d76b62` |
| DTB | `fbf606c939b855f06a11d66f3206be46552ba7ba0fe58240231d3b50a891aef8` |
| Stage-1 initramfs | `37465c5579547d5f00a0db8fbe5d30cea85a553ecaaff08d611d819ce934555f` |
| Original Firefox ESR 140.15 archive | `b47a780a07eb4cdc697a9e8b5d3cb7fd82ff9fdec7891dc3ade83463a3988f56` |
| Executed wide checkpoint baseline | `fc154e4d2deb72a9ca881320dce9da108cff8b2421b39d0dc112a6238ebec01a` |
| Generated guest source | `96c78cc5e846a6667b3cd5967a5daeaeeea7cdd265cba8582f0e3352d97cdc24` |
| Guest dmesg helper | `93c453f3ae57df4c2660a500eda8425bb120314aca9514ec4293b3f64a490eeb` |

## Collector correction and evidence completeness

The first attempt, retained under `target/firefox-dmesg-20260908/run-01`,
reproduced the command timeout but was classified `evidence_incomplete` because
no dmesg frame reached the host.  The guest printed its terminal failure marker
before the outer `finally` exported the collector; the host then terminated
QEMU while the bounded dmesg child was still being stopped.  This was an export
ordering defect, not observed kernel-log loss.

The runner now hooks the terminal failure print and exports the collector first.
A source-transformation regression failed before this change and passes after
it.  The second run produced:

| Artifact/property | Value |
| --- | --- |
| Output directory | `target/firefox-dmesg-20260908/run-02` |
| Host elapsed time | 805.840 seconds |
| Raw dmesg | 2,400,446 bytes; SHA-256 `79533b30ff99c0bff54b3ec23f6e96b8cbafe42919813d0ccfd8954c2b8c9403` |
| Collector loss | 0 discarded bytes, 0 discarded lines, no early exit |
| Collector shutdown | targeted SIGTERM, return code `-15` |
| Snapshot SHA-256, before | `4c891630fe3da82ab3a9b4b6aea0b6b08de02815d1e10f245b40d83f8deafb59` |
| Snapshot SHA-256, during | `a75258be1544f07a3562e3dcbd2e6e7a365314d151f053587d00892e4a156b95` |
| Snapshot SHA-256, after | `dcabcf16e54985abf0c58d522c8b6f0c7eee73958aecd7f345b26607b6eb0827` |
| Snapshot limitation | `fd_limit` only; ten processes retained in every phase |
| Parser state | no dmesg framing, actor, transport, or checkpoint errors |
| Fatal scan | no kernel panic, Oops, watchdog, OOM, or lockup marker |

All three snapshots identify the Firefox root as PID 80, PPID 1, start time
46693 ticks.  The identity and ten-process set remain stable.  The classifier
was recalculated in a fresh Python process from `dmesg.raw`, three snapshots,
actor JSONL and transport JSONL.  Its canonical JSON exactly equals the
immutable `classification.json`; both canonical values hash to
`f59a6a1791e49ff4a9861392572e4e70ba635c63e3e032e7f52d62d65f498b89`.

The syscall lifecycle logger exhausted its boot-wide 1024-record budget before
the selected request and emitted suppression summaries up to 512 suppressed
records.  The dmesg transport itself is complete, but absence of a later
lifecycle record must therefore not be interpreted as absence of a process
event.  Stable proc identities are the positive process-survival evidence.

## Runtime observations

All timings in this table use the transport's guest monotonic clock only.

| Command | Request ID | Result | Duration |
| --- | ---: | --- | ---: |
| NewSession | 1 | 739-byte response complete | 220.915 s |
| GetWindowHandles | 2 | 51-byte response complete | 0.156 s |
| Navigate | 3 | 25-byte response complete | 12.203 s |
| ExecuteScript | 4 | send complete; response header/body remain 0 bytes | 300.003 s to timeout |

Request 4's send-to-timeout interval is 299.829 seconds, so it had the complete
configured observation budget rather than the 28.720-second residual budget of
the earlier actor experiment.  `server.loaded`, `driver.loaded`, and
`parent.loaded` prove that the instrumented modules and marker channel loaded.
No selected-request `server.command`, `driver.enter`, parent query, child,
or response-queue marker exists, and the actor parser reports no malformed
record.  The earliest injected request marker is immediately after
`TCPConnection.execute` creates the response object.  Therefore the supported
boundary is: the client send returned, but Firefox did not observably enter
command execution.

The parent browser and multiple content processes are not wholly deadlocked.
Between the before and after snapshots, 18 of the 70 common Firefox-parent
threads and threads in six captured child processes advance their syscall
sequence.  This does not show application-level correctness, but it rejects a
claim that the entire browser stopped scheduling.

The parent `Socket Thread` (TID 182) provides a narrower clue:

- before: `ppoll` sequence 277;
- during: a `recvfrom(fd=11)` completed with `EAGAIN`, then indefinite
  `ppoll(nfds=3)` sequence 280 began;
- after: the same `ppoll` sequence 280 was still current.

Parent fd 11 is `socket:[265]`.  The snapshot does not contain the three
`pollfd` values or a peer mapping to the Marionette client, and the parent owns
many sockets.  Consequently this is compatible with a missing socket readiness
notification but is not proof of one.

## Source path and competing hypotheses

The pinned Firefox transport executes this chain:

```text
DebuggerTransport.ready
  -> _waitForIncoming
  -> async input stream asyncWait(currentThread)
  -> onInputStreamReady
  -> stream.available / _processIncoming
  -> _onJSONObjectReady
  -> TCPConnection.onPacket
  -> TCPConnection.execute
  -> despatch
```

The absent `server.command` marker is in the last two steps, so the first
unobserved interval is between async socket readiness/packet parsing and
`TCPConnection.execute`.  Parent-to-content actor IPC, JavaScript execution,
post-script event dispatch, and response queuing are downstream; this run gives
no positive evidence that request 4 reached them.

If fd 11 is the Marionette socket, the relevant Asterinas relationship is:

```text
client send
  -> StreamSocket::try_send
  -> TCP/interface polling
  -> peer CAN_RECV event
  -> StreamObserver::on_events
  -> Pollee::notify(IN)
  -> Poller wake
  -> do_poll repoll and ppoll return
```

`Pollee::poll_with` registers the observer before checking readiness and its
unit tests cover notify-before/during/after races.  Current code also contains
the earlier `PollScheduler` sentinel correction that distinguishes “no poll”
from `PollAt::Now`; that already-merged correction is not evidence of the
present cause.  The current root's one-shot TCP egress marker fired once during
startup, before request 4, so it cannot establish this selected send-to-notify
chain.  The current `tcp_poll` regression uses zero-timeout polls and sleeps;
it does not test a thread already blocked in indefinite `ppoll` when loopback
data arrives.

Remaining hypotheses, in order of the first missing boundary, are:

1. the accepted loopback socket does not make the blocked poller runnable for
   this arrival/order;
2. the kernel wakes correctly but Gecko's async-input callback or packet
   parsing does not dispatch the complete request;
3. the observed Socket Thread/fd 11 is unrelated to Marionette, so the current
   syscall clue is incidental.

The run makes downstream content-actor, futex, process-reaping, graphics, and
response-queue explanations poor *first-boundary* candidates.  It does not
prove those subsystems universally correct.  Instrumentation reproduced the
same first ExecuteScript failure seen with the current kernel, but startup
timing changed; no claim of zero observer effect is made.  An observer-disabled
control is not required by the approved stopping rule because instrumentation
did not turn the failure into a success.

## Single next microtest

Add one Linux-referenced regression beside `tcp_poll.c`, and run it on Linux
and Asterinas SMP4 with the same executable:

1. create one persistent IPv4 loopback TCP connection and accept it;
2. make the server socket nonblocking and drain it until `EAGAIN`;
3. have the server thread enter indefinite `ppoll` on exactly three descriptors:
   the accepted socket, a control pipe, and an inert descriptor;
4. after an acknowledged quiet gap, have another thread send one
   length-prefixed 1176-byte payload on the connected socket;
5. require `ppoll` to return for `POLLIN`, read the exact payload, drain back to
   `EAGAIN`, and repeat enough times to cross CPU scheduling on SMP4;
6. separately write the control pipe and require that wake path to work, so a
   generic poller failure is distinguishable from TCP readiness propagation.

The test should log only iteration, known fd numbers, send completion,
`ppoll` return mask, receive length, and bounded timestamps.  A Linux pass plus
Asterinas failure selects the kernel chain above without another 13-minute
Firefox run.  If both pass, the next probe belongs inside Gecko's async stream
callback/packet parser and must first map the actual Marionette socket; broad
kernel logging or another unchanged Firefox run would not be informative.

No semantic fix is proposed from the present evidence.
