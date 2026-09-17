# Firefox Daily-Use Gate Design

**Date:** 2026-09-17
**Status:** Approved conversationally; awaiting written-spec review

## Objective

Turn the existing scattered Firefox qualification tools into one bounded,
browser-centred workflow that answers two user-facing questions:

1. Which ordinary browser functions work in the current Asterinas image?
2. Where does the user-visible time go when typing, scrolling, navigating, or
   switching browsing contexts?

The workflow has one guest entry point and one summary report. It reuses the
current local fixture, Marionette client, composite workload, process sampler,
thread sampler, and boot timeline. It does not introduce a general operating
system benchmark suite.

## Current boundary

The repository already has independent evidence for:

- keyboard and pointer delivery through the physical interaction gate;
- deterministic keyboard, pointer, scroll, navigation, layout, canvas, image,
  cache, history, and multi-context browser work;
- Cookie, LocalStorage, SessionStorage, IndexedDB, WebAssembly, Worker, Canvas,
  Audio, Fetch, and controlled download checks;
- Firefox/Xorg identity, CPU, RSS, runnable wait, dispatch count, system load,
  and boot timeline collection;
- public HTTPS navigation through the network-enabled desktop.

These checks currently require different commands and produce separate
artifacts. The full web gate also depends on changing public pages and deletes
the WebDriver session at the end, while the composite collector intentionally
keeps Firefox alive. Neither is the right daily-use interface by itself.

The latest three-run physical profile shows a repeatable 4.68--4.84 second
interaction/layout phase and 2.61--2.70 second concurrent-resource phase.
Aggregate CPU busy remains below 49 percent and fixture concurrency remains
one. This does not support a broad CPU-affinity change; it selects browser
render/IPC wakeups and the serialized resource path for the next diagnostics.

## Approaches considered

### Extend the existing full public-web gate

This would minimize the number of executables, but the file is already large,
mixes deterministic and public inputs, and intentionally ends the WebDriver
session. Adding performance sampling and tab lifecycle to it would make
failure attribution and safe reuse harder. This approach is rejected.

### Drive the desktop entirely through X11 automation

This resembles human use and can cover browser chrome, but focus, window
manager state, coordinates, and rendering timing make it brittle. X11
automation also cannot prove that storage or WebAssembly produced the expected
result without a browser-side observer. It remains an optional final
acceptance layer, not the primary gate.

### Add a thin daily-use orchestrator

This is the selected approach. A small orchestrator calls existing validated
browser operations in one live Firefox session, runs existing low-overhead
samplers, and publishes a compact verdict. New code owns orchestration and
summary validation only; existing components continue to own fixture serving,
workload semantics, and procfs parsing.

## User interface

The Stage1 archive exposes one command:

```text
/run/asterinas-tools/browser-daily-use-gate \
  --firefox-pid PID --xorg-pid PID \
  --fixture-index-url http://HOST:17894/browser-quality/index.html \
  --evidence-dir /run/asterinas-browser-daily-use \
  --physical
```

The command uses reviewed defaults and accepts no arbitrary script, workload
URL, concurrency, or unbounded duration. It emits exactly one terminal line:

```text
ASTERINAS_BROWSER_DAILY_USE_PASS functions=N/N slow=M evidence_dir=PATH
```

or one bounded failure line. It never reboots the board, restarts Firefox,
deletes the Marionette session, rewrites partition 2, or changes the boot menu.

## Functional contract

The gate verifies these groups against the exact local fixture origin:

| Group | Required observations |
| --- | --- |
| document | JavaScript completion, Latin/CJK text, image and form DOM |
| storage | Cookie, LocalStorage, SessionStorage, IndexedDB write/read |
| execution | WebAssembly instantiate, Worker exchange, Fetch result |
| rendering/media | Canvas result and bounded audio metadata decode |
| navigation | form navigation, second document, back and forward |
| download | safe regular file, expected owner, exact size and SHA-256 |
| contexts | open a second controlled tab, select it, return, and close it |

Every group is either `pass`, `fail`, or `unsupported` with an enumerated
reason. A required local feature reported as unsupported makes the local gate
fail; unsupported physical HDMI scanout remains a measurement limitation, not
a browser-function failure.

Public HTTPS browsing is a separate optional phase. A network-enabled release
run records the exact URL, TLS verification, DOM readiness, and outcome for a
simple public page. External DNS, proxy, server, or anti-automation failure is
kept separate from the deterministic local verdict. A public failure cannot
erase the local evidence.

## Five user-visible performance categories

The result has five top-level categories rather than a general benchmark score:

1. **Startup:** existing guest-monotonic `BOOT_FIREFOX_EXEC` to
   `BOOT_FIRST_WINDOW_READY`, bound to the current Firefox PID.
2. **Input:** synthetic keyboard and pointer event-to-first/next-rAF p50 and
   p95. Synthetic timing is labelled and is not described as USB-to-HDMI
   latency. Optional physical evidence is referenced, not mixed into it.
3. **Scroll:** synthetic scroll event-to-first/next-rAF p50 and p95.
4. **Navigation:** local command duration plus valid response-to-DOM/load
   Navigation Timing intervals. The known negative `fetchStart` remains an
   explicit invalid field and is never clamped.
5. **Context switch:** open/select/return/close durations for one controlled
   second tab, with exact handle counts before and after cleanup.

The existing composite phase durations, fixture concurrency, Firefox/Xorg CPU,
RSS, runnable wait, dispatches, and limitations are included as supporting
attribution. They are not expanded into more top-level scores.

Initial responsiveness targets are diagnostic labels, not reasons to discard
otherwise correct evidence:

- input and scroll p95 at or below 100 ms;
- local navigation DOM readiness at or below 2 seconds;
- controlled context switch at or below 500 ms;
- no Firefox/Xorg identity change and no surviving extra context;
- observed fixture concurrency above one during the concurrent-resource phase.

The report marks a category `slow` when it misses a target. Regression gates
become strict only after a qualified physical baseline is committed.

## Diagnostics without log flooding

Normal mode keeps detailed syscall, futex, epoll, VM, and serial profiling
disabled. It records procfs counters and schedstat only.

An explicit diagnostic mode may consume a bounded kernel-log file produced by
the existing opt-in profilers. The summary retains only:

- the ten highest syscall count and elapsed-jiffy deltas for the Firefox PID;
- bounded slow-call counts grouped by syscall name;
- structured unimplemented/`ENOSYS` records when the kernel emitted them;
- malformed, truncated, or unavailable status.

The orchestrator does not infer an unsupported syscall from arbitrary warning
text and does not enable a profiler itself. If structured `ENOSYS` evidence is
not available, the report says `unsupported` instead of zero. A focused kernel
instrumentation change requires a separate measured hypothesis because the
current syscall profiler can perturb Firefox.

## Data flow and artifacts

The gate performs one ordered run:

1. validate the evidence directory, exact PIDs/start times, fixture origin,
   timeline identity, and original Firefox window;
2. create one verified Marionette session;
3. start existing system and thread samplers and wait for both initial
   snapshots;
4. run local capability, storage, navigation, download, performance, tab, and
   composite phases;
5. stop samplers with the existing bounded final-snapshot handshake;
6. verify Firefox/Xorg identity and one-window cleanup;
7. atomically publish `browser-daily-use-result.json` and retain the existing
   component artifacts in the same private directory.

The summary contains only canonical relative artifact names, byte sizes, and
SHA-256 identities. Browser-performance, guest-monotonic, kernel-log, and host
fixture clocks remain separate. No subtraction occurs across clock domains.

On failure, a private checkpoint records completed functional groups and their
bounded reasons. Staging files are unique to the run ID and never replace a
previous result. Background sampler expiry fails the run and cannot publish
late data into the final artifact names.

## Code boundaries

- `browser_daily_use_contract.py` owns the finite schema, exact functional
  groups, performance-category validation, target labels, and report creation.
- `browser_daily_use_gate.py` owns the one-session sequence and safe artifact
  publication. It imports existing fixture and sampler helpers rather than
  duplicating their implementations.
- `megrez_network_fixture.py` changes only if an exact missing local resource
  is required. Existing index, timing, second-page, download, and workload
  resources should be reused first.
- `build_stage1.sh` packages the two small modules into the existing Stage1;
  normal Debian rootfs and partition 2 remain reusable.
- `firefox_fast_check.sh` includes the new contract/orchestrator tests.

No kernel file, DRM file, public-web gate semantics, or network-stack behavior
changes in the first implementation milestone.

## Testing and qualification

Development is test-first:

1. contract tests reject missing, extra, reordered, oversized, non-finite,
   cross-clock, identity-changing, or falsely supported evidence;
2. mocked Marionette tests prove exact phase order, safe tab cleanup, checkpoint
   retention, no DeleteSession, and bounded failure;
3. sampler tests prove initial/final coverage and no late publication;
4. Stage1 packaging tests prove executable mode and exact archive membership;
5. the existing Firefox fast check remains green;
6. a cached four-hart RISC-V QEMU run executes local `smoke` mode;
7. one physical boot executes local `profile` mode, then an optional simple
   public-page phase, without rootfs deployment or automatic reboot.

The physical run is accepted only when the current Firefox and Xorg identities
remain stable, all required local functions pass, extra tabs are removed,
samplers cover the full interval, and the board remains interactive afterward.

## Optimization boundary

The first gate commit is measurement and functional consolidation. It does not
claim a speedup.

The first optimization is selected from the resulting evidence:

- serialized local resources select socket/epoll/browser connection tracing;
- high runnable wait in WebRender/Compositor/IPC selects wakeup-to-run tracing;
- high kernel CPU or real fault growth selects a focused kernel counter and
  regression;
- low kernel cost with high browser time selects Firefox/Xorg software-render
  work while keeping the DRM provider boundary unchanged.

Each optimization needs one failing regression or controlled A/B test and must
rerun the identical daily-use workload before it is merged.

## Acceptance criteria

The first milestone is complete when one command produces one bounded report
that:

- lists every required local browser function with an evidence-backed verdict;
- reports all five performance categories without mixing clock domains;
- retains composite and procfs attribution with explicit limitations;
- preserves the running desktop and original Firefox session;
- runs in QEMU and on the existing physical image without rebuilding or
  rewriting the Debian root partition;
- identifies the next focused optimization without claiming that DRM, page
  faults, networking, or Firefox performance is already solved.
