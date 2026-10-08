# Firefox actor-stage diagnostic experiment

## Result and claim boundary

The experimental Firefox archive ran, but this batch did **not** locate the internal ExecuteScript failure or fix Firefox.
The full serial transcript contains `server.loaded`, `driver.loaded`, and `parent.loaded` from Firefox PID 81.
It contains no command-stage or child-actor marker.
The original live collector incorrectly reported zero markers because it did not handle systemd's `browser-web-firefox[95]:` prefix and did not scan across protocol checkpoints.
PID 95 is the tail forwarder, not the browser.
Those collection defects are now covered by tests and corrected; the original run and its summary remain immutable.

NewSession and Navigate took substantially longer in this run than in the quiet baseline.
The unchanged absolute setup deadline left ExecuteScript only 28.720 seconds after send completion.
Thus absence of a command-stage marker is **not** sufficient evidence that TCP lost the request, the handler never ran, or a content process deadlocked.
There is no child-marker positive control or target BrowsingContext/PID mapping yet.

This was one QEMU experiment, not a physical-board run or browser acceptance.
No kernel source, sandbox policy, browser/page deadline, page, dependency version, or PR was changed in this batch.
No package downloads, Cargo OSDK installation, image/container deletion, or commits were performed.

## Implementation

`tools/riscv/diagnostics/firefox_actor_overlay.py` transforms exactly four allowlisted members of the locally installed Firefox ESR 140.15 archive.
It rejects a different source hash and missing/ambiguous source anchors, preserves member order/content outside the allowlist, produces deterministic ZIP bytes, and refuses to overwrite an existing archive.
`firefox_actor_trace.js` supplies the injected marker helper.
The marker budget is 128 records per module per process, enabled only by `ASTERINAS_FIREFOX_ACTOR_DIAGNOSTICS=1`.
Fields are version, static stage, source PID, request ID, BrowsingContext ID, and target PID when available.
No script, URL, arguments, return body, nonce, or error payload is logged.

Instrumented boundaries are:

```text
server command
  → driver entry → prompt handling → actor selection
  → parent query entry → query submitted
  → child receipt → script complete → next event complete → reply serialized
  → parent query complete → driver complete → response queued
```

The installed child implementation explicitly awaits `executeSoon` after script completion and before result serialization.
The isolated JavaScript tests execute that actual patched receive method with controlled evaluation/event scheduling.
They verify that script completion is not confused with reply completion and preserve the original returned value/error object.
Other tests exercise the actual driver/parent methods to verify request correlation, target PID, unchanged timeout, and default-off behavior.
Node provides these isolated tests, not a Gecko runtime substitute.
The already available host Node binary was copied to `/tmp/asterinas-firefox-diag-node` in the persistent container; nothing was downloaded or installed into the image.

`firefox_actor_records.py` validates markers, handles the exact systemd forwarder prefix, and scans a complete growing transcript with its own cursor.
It rejects malformed JSON objects/scalars and unknown stage names without interrupting the protocol.
The revised experimental runner scans during protocol reads and after final serial drain.
Receipt timestamps are collector times, not Firefox execution timestamps.

## Frozen artifacts

All paths below are relative to the graphics worktree:
`/home/ubuntu/.config/superpowers/worktrees/asterinas/megrez-physical-graphics-current-main`.

| Input | SHA-256 |
| --- | --- |
| Kernel `target/firefox-diagnostics-20260908/kernel.Image` | `66b61ff120be1c033c20777b6cca86fe86bf8fe775a9df6d90e7ec276730628c` |
| Base root `target/firefox-diagnostics-20260908/browser-rootfs-final/debian-root.ext2` | `8598c4e07f542e714d623270737de41526f60ff7c5713ab1395821abab665b14` |
| Original Firefox archive | `b47a780a07eb4cdc697a9e8b5d3cb7fd82ff9fdec7891dc3ade83463a3988f56` |
| Experiment archive `target/firefox-diagnostics-20260908/actor-payload/omni.ja` | `29ec254f3a6284e1d2bcf6c816a2c00083f0df44d001b9e05b359f561621a5cb` |
| Experiment root `target/firefox-diagnostics-20260908/browser-rootfs-actor/debian-root.ext2` | `c7ec736b9ac07494fc8de490d4315701115a93769ae26382fa97cb85c7673e94` |
| Executed actor runner, preserved in `actor-payload/browser_actor_experiment.py` | `7576dac8464f2d15b81bd027263be6ebb860404318f1575285f974f10eb14937` |
| Wide checkpoint runner | `fc154e4d2deb72a9ca881320dce9da108cff8b2421b39d0dc112a6238ebec01a` |

Packaging took 4.835 seconds and changed exactly two rootfs files: `/usr/lib/firefox-esr/omni.ja` and the diagnostic Firefox launcher.
The source archive was also extracted from the base ext2 image and checked against its pinned hash.
The base browser profile directory was empty, without an existing profile startup cache.
The diagnostic launcher adds dump permission only under the explicit diagnostic switch.
The run-copy `user.js` confirms both BASIC_ONLY preferences and that dump permission were active.

Kernel, U-Boot, DTB, stage1 initramfs, package lock and package-checksum hashes match the quiet wide baseline.
The root image and manifest intentionally differ.
Unchanged package provenance does **not** mean the modified Firefox payload is an unmodified Debian package.
Per-member and launcher hashes are in `actor-payload/provenance.json`.

The source helper and collector were improved after this run during review.
The experiment used the original preserved helper/transformer/runner, not those later revisions.
The revised helper snapshots its source once per transformation and accepts integer request ID zero using a null absent-request sentinel.
These changes have unit coverage but have **not** been exercised in another QEMU run.

## Runtime evidence

Output directory: `target/firefox-diagnostics-20260908/qemu-actor-stages`.
Original full transcript: `physical-graphics-qemu.serial.log`, SHA-256 `972d48211b9f4a688ccd2af97fba45b835ff0c026fc55a7380208aca132413b1`.
The corrected offline recovery is `actor-recovered-evidence.json`; it supplements, rather than overwrites, `actor-experiment.json`.

Durations below use each run's guest monotonic transport timestamps from send completion to complete/failure.
The clocks are not mixed with kernel jiffies or host time.

| Command | Quiet wide baseline | Actor experiment | Actor result |
| --- | ---: | ---: | --- |
| NewSession | 169.464 s | 419.231 s | Complete frame |
| Navigate | 5.178 s | 126.155 s | Complete frame; pageLoadStrategy remains none |
| ExecuteScript, ID 4 | 407.048 s | 28.720 s | Timeout; response header 0 bytes |

The host run lasted 871.895 seconds and exited 1 before READY.
Before-capture exceeded its existing worker timeout and returned 124 without a completed snapshot.
The 30-second delayed worker was cancelled when the command's remaining deadline expired.
Only the after-capture completed: 9 processes, 155 distinct sampled thread records, 245210 bytes, 6.006 seconds; its sole collection limitation was `fd_limit`.
All sampled process/thread identities passed their within-capture checks.
This is not a three-checkpoint comparison and cannot show process/thread progress across the request.

The after-capture identifies Firefox as PID 81, start ticks 44583, and the tail forwarder as PID 95, start ticks 47788.
Firefox stdout and the tail's input descriptor both showed offset 2282 at their respective non-atomic sample times.
The tail was waiting on its inotify descriptor; this alone does not prove a lost notification or a working end-to-end log channel for an unobserved command.
The persisted disk stderr inode contained only its initial 188 bytes after QEMU exit, so offline disk content cannot replace the fuller serial transcript.

Other logs include a child process 337 exiting on signal 9 and a delayed graphics WaitFlushedEvent warning.
No captured mapping links that child to the requested browsing context, and neither log establishes the root cause.
The slower startup/navigation also prevent treating this run as an interchangeable timing control for the quiet baseline.

## Verification and next checkpoint

Eighty-three focused tests passed in the persistent container with ResourceWarning promoted to an error.
These include 20 actor/helper/parser/runner tests and 63 existing snapshot/transport/physical-gate tests.
The runner replay checks protocol scanning, final drain, malformed markers, unchanged failure status, and non-acceptance classification without another boot.
The initial missing implementation and the review regressions were observed failing before their fixes.
JavaScript-dependent tests explicitly skip when the cached archive or Node is unavailable; the actual verification run had both.

Independent combined maintainability/development/security review is recorded under `target/firefox-diagnostics-20260908/actor-review`.
Its four findings were confirmed: startup collection gaps, malformed JSON exception, non-snapshot helper provenance, and request ID zero suppression.
Tests and implementation address each; the actual systemd prefix was additionally validated against this run's transcript.
The follow-up independent pass found stale-record mixing on output-directory reuse and an installed-archive check implemented with an assertion.
Both were reproduced and corrected: the diagnostic runner now rejects nonempty output directories, and preparation explicitly checks the installed hash before generating the payload, including under Python `-O`.
The final two fixes were controller-verified by regression tests; no third independent pass or second QEMU run is claimed.
Review history is retained under `actor-review-followup`.

The final verification command was:

```sh
docker exec -w /root/asterinas \
  -e ASTERINAS_TEST_NODE=/tmp/asterinas-firefox-diag-node \
  asterinas-dev-v1-4f054ba7e4d3-a96860817385 \
  python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_firefox_actor_diagnostics \
  tools.riscv.tests.test_firefox_actor_records \
  tools.riscv.tests.test_firefox_diagnostic_snapshot \
  tools.riscv.tests.test_marionette_diagnostics \
  tools.riscv.tests.test_physical_graphics_gate \
  tools.riscv.tests.test_physical_graphics_qemu_gate
```

Fresh hashes confirmed that the kernel, base root, experimental root and preserved executed runner remain unchanged.
`git diff --check` passed, and no RISC-V QEMU process remained in the persistent container.

The next informative experiment should first establish a **successful pre-navigation `return 1` marker-chain positive control**, then execute the same probe after navigation.
It should explicitly record remaining setup budget and reject an insufficient observation window as inconclusive, without extending the existing deadline.
Snapshot collection should remain out of the command's critical path.
Until a successful command proves the request/child markers and output channel, missing stages must not be translated into a TCP, futex, epoll, or child-lifecycle fix.
