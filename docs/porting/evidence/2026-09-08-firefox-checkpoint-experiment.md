# Firefox content-command checkpoints

## Approved question and fixed inputs

The user approved one bounded QEMU investigation on 2026-09-08:
locate the navigation-to-ExecuteScript stall using before/during/after snapshots.
This experiment does not change kernel behavior, browser packages, the page,
the existing setup deadline, or physical/trusted-input acceptance.
It does not boot the physical board or integrate any PR.

Use the retained diagnostic kernel `66b61ff120be1c033c20777b6cca86fe86bf8fe775a9df6d90e7ec276730628c`
and packaged rootfs `8598c4e07f542e714d623270737de41526f60ff7c5713ab1395821abab665b14`.
Keep QEMU virt, SMP=4, 2 GiB RAM, and writeback on its disposable root copy.
Enable `asterinas.syscall_diag=1`, lifecycle `loglevel=info`, and the redacted
Marionette transport diagnostics. Preserve the 600-second setup deadline.
The diagnostic build/rootfs differ from the earlier uninstrumented witness;
this first run is localization evidence, not a single-variable causal comparison.

## Procedure and stopping conditions

- [x] Verify input hashes and offline container availability.
- [x] Test checkpoint ordering, original-error preservation, bounded serial
  framing, and guest source/serial command size before launching QEMU.
- [x] Capture the selected Firefox tree immediately before the first
  ExecuteScript, after 30 seconds if still outstanding, and after its return/error.
  The delayed collector runs in an independent process with an outer timeout.
  Also capture only the identified client's status and custom syscall file.
- [x] Transfer bounded compressed/checksummed snapshots to the host immediately.
  Record collection times in guest monotonic time and reception times in host
  monotonic time separately. Preserve explicit partial coverage and identity checks.
- [x] Stop after the observed command; never issue input or label this experiment
  browser/physical acceptance. Stop a delayed collector if the command returns early.
  Retain a missing `during` checkpoint explicitly if there was no sustained wait.
- [x] Interpret transport progress, surviving process/thread identities, outstanding
  calls, and lifecycle suppression. An idle epoll/futex alone is not a deadlock.
- [x] Record results and select at most one next code path based on evidence.

Host outer timeout is 1200 seconds with a 10-second termination grace.
Guest collector limits remain 16 processes, 128 threads, 64 fds, 262144 bytes,
and three seconds of collection; an external timeout covers uninterruptible reads.
If these limits prevent useful collection, retain that failure before adjusting
the diagnostic budget in a separately identified run.
The existing container and all immutable input images are retained.

## Results

### First attempt: global logging contaminates startup

`qemu-checkpoints-on/` was stopped after 500.523 host seconds before reaching
the browser checkpoint. Its 497-line, 97988-byte serial log contains verbose
VirtIO block-request/IRQ records and clock-source unreliability warnings.
A read-only QMP screendump shows this output filling the framebuffer.
No Firefox checkpoint was reached; this is not a Firefox failure reproduction.

The first driver's source is retained as `browser_checkpoint_experiment-info.py`,
SHA-256 `57a69cb4e9b9dc22d91f978f5fa595746125b4d85b5bd8ed008547e109fb07d0`.
TERM is deferred by the existing runner until cleanup; an INT then entered
normal teardown. The runner records exit 143, and its QEMU process was verified gone.
The shell tee pipeline's status 0 does not mean the experiment passed.

The second attempt keeps exactly the same kernel/rootfs and restores
`loglevel=off`, retaining `asterinas.syscall_diag=1` and client-side diagnostics.
This intentionally loses lifecycle logs; absent exits must not be inferred
from that missing channel. A regression now checks that the diagnostic runner
does not enable global I/O logging. No kernel source change or rebuild is needed.

### Actual Firefox package source

The retained ESR 140.15 `omni.ja` has SHA-256
`b47a780a07eb4cdc697a9e8b5d3cb7fd82ff9fdec7891dc3ade83463a3988f56`.
Its `chrome/remote/content/marionette/driver.sys.mjs:985` delegates script
execution to the browsing-context actor after user-prompt handling.
`actors/MarionetteCommandsParent.sys.mjs:167` sends the actor query;
`actors/MarionetteCommandsChild.sys.mjs:213` awaits script execution and then
at line 284 waits for an additional `executeSoon` event before serializing
the response. Therefore an outstanding ExecuteScript does not by itself prove
that the JavaScript body has not executed. These are source relationships,
not evidence of which stage the observed Firefox reached.

### Quiet attempt: reproduced at the response-header boundary

`qemu-checkpoints-quiet/` finishes in 826.351 host seconds, with driver exit 1,
`passed=false`, and no browser/physical acceptance.
All three checksum-validated frames arrive without framing errors.
The driver remains `browser_checkpoint_experiment.py`, SHA-256
`2bc3a52a067dae67437d23ab8068265467539cc69678b5fb5a7b0e3e418d1fb7`.

| Host elapsed seconds | Observation |
| ---: | --- |
| 466.771 | NewSession completes, 739 response-body bytes received |
| 502.931 | Navigate completes, 25 response-body bytes received |
| 510.434 | Before snapshot saved |
| 510.852 | ExecuteScript request 4 send completes |
| 547.276 | During snapshot saved |
| 820.056 | Request 4 times out at `response_header`, zero header/body bytes received |
| 825.855 | After snapshot saved |

The selected Firefox is PID 70, start time 37796 ticks.
The client is PID 252, start time 181106 ticks.
Root identity at capture entry matches across all three snapshots.
The first two captures exhaust three seconds before final root validation;
the last validates the parent after collection, but still lacks its descendants.
These are explicitly partial, non-atomic captures, not complete process-tree evidence.

| Parent thread | Before sequence | During sequence | After sequence | Interpretation |
| --- | ---: | ---: | ---: | --- |
| 70, main | 102373 | 102572 | 104336 | Continues making calls; sampled in futex waits |
| 174, IPC I/O Parent | 5178 | 5456 | 7588 | Continues receiving and polling; last recvmsg is EAGAIN |
| 176, Timer | 2075 | 2188 | 3049 | Continues wake/wait activity |
| 178, Socket Thread | 270 | 273 | 273 | Same ppoll persists from during to after |
| 212, Renderer | 1926 | 6322 | 41161 | Continues writev/poll activity; not a CPU utilization measurement |

The listed threads pass before/after thread identity checks within each capture.
Their progression argues against a whole-parent-process deadlock.
It does not establish that the Marionette handler dispatched or completed its actor query.
Socket Thread's last receive on fd 34 returned EAGAIN;
it then waits indefinitely in ppoll. This is compatible with waiting for new
socket work and is not proof of a missed wakeup.
Parent fd 14 is an epoll instance with its interest list captured;
fd 29 is `pipe:[74]`, and fd 34 is `socket:[274]`.
Only a partial parent fd set is available, not an IPC peer graph.

The collector's three-second budget is inadequate for this Firefox tree:
the before capture has 43 usable distinct TIDs, during has 69, and descendants
are omitted after ancestor revalidation cannot finish.
The during/after protocol frame is not a substitute for those missing processes.

### Wider capture, same system inputs

The follow-up driver `browser_checkpoint_experiment-wide.py` changes collection
limits only: 15 seconds, 384 threads, 256 fds, and 1 MiB read budget.
The process cap stays 16 and each file remains capped at 8192 bytes.
External worker timeouts become 22 seconds plus a two-second kill grace;
the caller additionally bounds its wait at 27 seconds.
Browser setup deadline, page, kernel/rootfs, and loglevel are unchanged.
The seven focused experiment tests pass, including the explicit quiet-log and
finite-wide-budget checks. This run is necessary to cover content descendants,
not an attempt to cure Firefox by extending its command timeout.

The completed wider run is retained under `qemu-checkpoints-wide/`.
It finishes in 826.864 host seconds with driver exit 1 and no acceptance.
NewSession completes at 386.612 seconds and Navigate at 392.121 seconds.
Request 4 is sent completely at 406.418 seconds and times out at 813.467 seconds:
again `response_header`, `header_bytes=0`, `body_received=0`.
The before/during/after snapshots reach the host at 404.206, 456.819, and
826.338 seconds respectively; all frames pass their checksums.

All eight input hashes match the quiet attempt exactly.
The wider driver's SHA-256 is
`fc154e4d2deb72a9ca881320dce9da108cff8b2421b39d0dc112a6238ebec01a`.
The same ten `(pid, ppid, start_time_ticks)` identities occur in all three captures.
All captured processes and threads pass their within-capture identity checks.
There are 157, 154, and 148 distinct sampled TIDs respectively;
thread-count changes across captures are not by themselves evidence of a crash.
Collection takes 6.423, 12.782, and 7.043 guest monotonic seconds and reads
250944, 248278, and 248277 bytes respectively.
Each snapshot reaches its 256-fd cap; `fd_limit` is the sole completeness limitation.
Thus process/thread coverage is useful, while the fd/IPC peer graph remains partial.

| Content process / thread | Before sequence | During sequence | After sequence |
| --- | ---: | ---: | ---: |
| PID 291, main / ppoll | 6862 | 6876 | 7371 |
| PID 291, IPC TID 292 / epoll_pwait | 2507 | 2507 | 2515 |
| PID 325, main / ppoll | 3890 | 4939 | 12768 |
| PID 325, IPC TID 326 / epoll_pwait | 544 | 727 | 2085 |
| PID 340, main / ppoll | 3168 | 3168 | 3179 |
| PID 340, IPC TID 342 / epoll_pwait | 394 | 394 | 394 |
| PID 359, main / ppoll | 3476 | 4515 | 12351 |
| PID 359, IPC TID 361 / epoll_pwait | 393 | 573 | 1934 |

The four processes are named `Web Content` by procfs.
The parent, WebExtensions, Socket, RDD, Utility, and the wrapper's tail process
account for the other six identities. No sampled whole content process disappears.
PID 325 and PID 359 continue main-thread and IPC activity throughout the wait.
An unchanged epoll call in another process can be legitimate inactivity;
the snapshots do not identify which content process owns the requested page.

## Conclusions and the next discriminating boundary

Established: two quiet diagnostic runs reproduce the zero-response-header
ExecuteScript timeout; the parent is not wholly stalled, and the wider run
does not show all content processes exiting or ceasing syscall activity.
This excludes neither a particular IPC delivery/wakeup defect nor a browser-side
unresolved operation. Client send completion is not proof of Firefox handler entry.
There is no basis yet for selecting a TCP, futex, epoll, or VM fix.
No on/off observer-overhead benchmark under Firefox load was performed.

The next scoped probe should follow the actual packaged Firefox actor path:
associate the Marionette request with its BrowsingContext and target process,
then mark parent dispatch, child receipt, script completion, post-script event
completion, and parent response. Markers must be opt-in, bounded, scalar-only,
and transferred to the host without depending on the stalled command.
Do not log script bodies, URL queries, return payloads, or full memory.
Those boundaries distinguish a request never dispatched, an unhandled child
message, an unresolved script/event task, and a response not returned.
Only then select a kernel primitive and its targeted regression if indicated.
This browser-side probe is not implemented in this batch.

## Final verification and scope

`checkpoint-wide-validation.log` records fresh assertions over exact transport
failure fields, three captures, all eight input hashes, matching ten-process
identities, thread-level identity checks, and the sole fd completeness limitation.
The experiment driver suite passes seven tests; the collector, transport,
physical guest and QEMU contract suites pass 63 tests in 0.413 seconds.
`git diff --check` reports no whitespace errors.
These checks do not establish Firefox correctness or physical acceptance.

The requesting-code-review skill's independent audit could not start because
the agent tool reported a thread limit. The final checks were performed by
the controller, not an independent reviewer; no merge-readiness claim is made.
All QEMU processes from this batch have exited. The persistent container,
immutable input images, run copies, logs, and screenshots are retained.
This batch changes only experiment artifacts and this evidence record;
there is no additional kernel implementation change, package download,
OSDK installation, physical-board operation, commit, push, or PR integration.
