# Firefox Actor Stage Diagnostics Implementation Plan

> **For agentic workers:** Use inline execution with review checkpoints. Preserve the existing dirty graphics worktree and all frozen evidence.

**Goal:** Locate the first unobserved Firefox-internal boundary for the already-reproduced ExecuteScript response-header timeout.

**Architecture:** Offline, hash-pinned transformation of four modules in the installed Firefox ESR 140.15 archive. Scalar-only markers follow command dispatch, prompt handling, actor query, child execution, next-event completion, serialization, and response queueing. They are disabled unless `ASTERINAS_FIREFOX_ACTOR_DIAGNOSTICS=1` and bounded to 128 records per module per process. The existing stderr mirror and checkpoint experiment collect evidence independently of Marionette.

**Tech Stack:** Python standard-library ZIP tooling, Firefox system JavaScript modules, cached Node for isolated JavaScript tests, existing ext2 overlay and persistent QEMU container.

## Approved scope and alternatives

The user approved tracing these internal boundaries after the checkpoint experiment. Broad trace logging would expose payloads and add considerable I/O; another syscall-only capture cannot identify the actor target. Use targeted markers instead. Do not change kernel behavior, sandbox policy, command/page deadlines, or the page. The modified Firefox archive is an experimental payload, not an unchanged Debian package or an acceptance artifact. No physical boot, network download, image deletion, commit, or PR integration is part of this batch.

## Execution

- [x] Add `tools/riscv/tests/test_firefox_actor_diagnostics.py`: reject the wrong source hash; reject ambiguous/missing anchors; preserve archive members and deterministic bytes; test default-off, exact opt-in, bounded numeric-only records and failed sinks; parse patched modules; execute the actual child receive method with controlled evaluator/event scheduling to check stage order and original error results.
- [x] Run the tests in the persistent container and observe the expected missing-implementation failure.
- [x] Add `tools/riscv/diagnostics/firefox_actor_trace.js` (injected helper) and `firefox_actor_overlay.py` (exact source edits, archive transformation, provenance). Leave the production rootfs overlay untouched. Output only to a new directory; refuse overwrites.
- [x] Run the same tests with the local pinned archive and cached Node. The actual Firefox runtime remains a separate QEMU integration check; Node does not emulate Gecko. Eleven focused tests pass with the explicit cached Node; nine JavaScript/archive tests skip explicitly when it is absent.
- [x] Materialize a new derived root from `target/firefox-diagnostics-20260908/browser-rootfs-final`, replacing only the archive and adding dump permission to the diagnostic launcher under the same explicit environment switch. Record original and modified hashes and per-module hashes. Packaging took 4.835 seconds; the source archive extracted from the base image matches the pinned hash, and its browser profile has no existing startup cache.
- [x] Reuse the wide checkpoint runner with the same kernel and timeouts, adding only the Firefox diagnostic environment and host prefix forwarding. The bounded run exited 1 after 871.895 seconds. Three parent-process module-load markers were recovered from the complete transcript; only the after-snapshot completed. Request-stage correlation was not established.
- [x] Validate framing/input hashes, inspect the last observed stage, and write the evidence and limitations. Preserve all outputs and verify QEMU cleanup. See `docs/porting/evidence/2026-09-08-firefox-actor-stage-experiment.md`. Two independent review passes identified six confirmed issues, repaired with regression tests; the final focused run passed 83 tests.
- [ ] Establish a successful pre-navigation command marker-chain positive control, then correlate post-navigation request/BrowsingContext/PID with bounded snapshots. Reject an insufficient remaining observation window as inconclusive instead of extending deadlines. This remains the next experimental checkpoint, not a completed Firefox diagnosis.

## Interpretation rules

Markers describe completion of their named code boundary, not wall-clock deadlines or successful delivery across the next boundary. `response_queued` means the transport enqueue returned, not that TCP delivered bytes. Missing child receipt is interpretable only after validating that the child marker code loaded and its output path works. A source/target PID is not a persistent identity until matched with proc start ticks. Logging can perturb timing; the quiet baseline remains the comparison and not an interchangeable acceptance result.
