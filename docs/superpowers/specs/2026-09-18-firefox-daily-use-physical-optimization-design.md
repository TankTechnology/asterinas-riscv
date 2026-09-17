# Firefox daily-use physical optimization design

**Date:** 2026-09-18

**Status:** Approved for implementation planning

**Milestone:** Physical daily-use baseline, attribution, and one-variable A/B

## Goal

Use the current `main` kernel and the Firefox daily-use `profile` workload on
the physical four-hart Megrez board to establish a repeatable baseline, identify
one concrete system bottleneck, implement one evidence-selected optimization,
and compare the same workload before and after the change.

The primary user-visible metrics are keyboard and pointer input p95, scroll p95,
local navigation response-to-DOM, and controlled context-switch durations.
Startup remains a secondary metric.
CPU runtime, completed runqueue wait, dispatch counts, system utilization, and
phase durations provide attribution; they are not substitutes for the primary
metrics.

## Non-goals

- Do not claim USB-to-HDMI latency from browser `performance.now()` samples.
- Do not use a public website as the optimization workload.
- Do not infer page-fault cost from the current placeholder procfs fields.
- Do not introduce affinity, futex, page-cache, or scheduler changes before the
  physical baseline repeatedly identifies the corresponding mechanism.
- Do not implement a general `perf_event_open` subsystem in this milestone.
- Do not rewrite partition 2 or modify the user's dirty `main` worktree.

## Existing evidence

The retained 2026-09-16 physical profiles identify the Firefox main thread as
the largest CPU consumer while Xorg remains below 0.04 average cores.
The two hottest Firefox threads were already observed on different harts, so
those profiles reject blind CPU-affinity tuning.
The detailed syscall and fault diagnostics increased a QEMU launch by about
36%, making them unsuitable as the first physical profiler.

The daily-use gate now supplies one bounded workload with seven functional
groups, five performance categories, process identity checks, system sampling,
and per-thread schedstat sampling.
It is packaged in Stage1 but is not yet connected to the fixed-action physical
control protocol or to a host evidence receiver.

## Considered approaches

### Baseline first, then add only missing attribution

This is the selected approach.
Run three qualified daily-use profiles with the current kernel, correlate the
primary metrics with phase, CPU, and runqueue evidence, and add sampled-PC or a
focused diagnostic only if the existing evidence cannot name a mechanism.
This obtains an uninstrumented baseline before adding profiler overhead.

### Implement sampled-PC before the baseline

A bounded sampled-PC facility can distinguish repeated user and kernel
instruction regions, but it changes the timer and per-thread hot paths before a
clean baseline exists.
It remains the next attribution tool when schedstat and phase evidence are
insufficient, not an unconditional prerequisite.

### Optimize a previously suspected mechanism immediately

Futex wakeups, page-cache behavior, software rendering, and scheduler policy all
have plausible costs.
None is admitted from the current evidence alone, so changing one now would be a
hypothesis-first optimization and is rejected.

## Isolation and provenance

All implementation work occurs on `codex/firefox-daily-use-perf` in a separate
worktree based on merged `main` commit `e96ec6b97`.
The existing dirty `main` worktree and its untracked board evidence remain
untouched.

Every physical run records the commit, uncompressed kernel SHA-256, Stage1
SHA-256, DTB SHA-256, immutable rootfs manifest SHA-256, board serial identity,
four-hart configuration, fixture address, display provider, Firefox/Xorg PID
and start-time identities, host experiment ID, and gate run ID.
The browser home is volatile and every run uses a fresh evidence directory.
One profile is performed per boot so browser downloads, caches, sessions, and
startup timeline records cannot leak across repetitions.

## Fixed guest action

Extend `/run/asterinas-tools/physical-graphics-control` with one closed action:

```text
daily-use <experiment-id> <timeout-seconds> <expected-firefox-pid>
```

The experiment ID is exactly 32 lowercase hexadecimal characters.
The timeout is positive and at most 120 seconds.
The action rejects an unexpected Firefox identity, a non-unique Xorg PID, an
existing evidence directory, or a missing local fixture configuration.

The action invokes the Stage1-bound daily-use gate inside the Firefox network
namespace with:

```text
--mode profile --physical
```

It passes the verified Firefox and Xorg PIDs, the exact physical fixture URL,
and `/run/asterinas-browser-daily-use-<experiment-id>`.
It does not accept an arbitrary command, path, URL, mode, or artifact list from
the host.
It does not restart Firefox/Xorg, delete the Marionette session, reboot the
guest, or write the root partition.

After the gate terminates, the action runs the Stage1-bound evidence uploader
in the same network namespace and emits one terminal serial status bound to the
experiment ID.
A gate failure remains a failed experiment even if its checkpoint uploads
successfully.

## Evidence upload contract

The existing local fixture server gains a separate one-shot endpoint:

```text
POST /browser-quality/daily-use-evidence/<experiment-id>
```

The server instance is configured with one expected experiment ID and the exact
board peer address.
It rejects query strings, transfer encoding, absent or repeated content length,
zero or oversized bodies, a different peer, a different experiment ID, and a
second upload.
The maximum canonical JSON body is 2 MiB.

The guest uploader reads only the closed daily-use artifact-name set.
It opens regular files without following symlinks, enforces mode and size
bounds, reads each file completely, and encodes this exact-schema bundle:

```json
{
  "schemaVersion": 1,
  "experimentId": "<32 lowercase hex>",
  "gateRunId": "<32 lowercase hex>",
  "outcome": "pass",
  "artifacts": [
    {
      "name": "browser-fixture-capture.json",
      "bytes": 1,
      "sha256": "<64 lowercase hex>",
      "base64": "..."
    }
  ]
}
```

On success, `artifacts` contains the six canonical component files in contract
order followed by `browser-daily-use-result.json`.
The uploader validates the result contract and requires its six artifact sizes
and hashes to match the payloads before sending.
On failure, it may upload only `browser-daily-use-checkpoint.json` with
`outcome` equal to `fail`; missing failure evidence never converts failure to
success.

The host decodes the bundle only after exact-schema validation, verifies every
base64 payload, size, SHA-256, result run ID, artifact manifest entry, and
experiment ID, then publishes files privately into a new host run directory.
It never treats the upload endpoint's HTTP success alone as experiment success.

## Host physical orchestrator

Add a dedicated host runner that reuses the current physical graphics boot,
artifact, debug-console, browser-start, and recovery primitives.
It starts the fixture server on `10.100.19.216:17894`, restricted to the board
peer, before booting the guest.
It generates the experiment ID, waits for graphical readiness, sends only the
fixed daily-use action, waits for both the terminal serial status and matching
evidence upload, and then observes the bounded recovery-to-U-Boot path.

The host output directory is exclusive and private.
It contains the seven successful gate artifacts, serial transcript, fixture
summary, deployment identities, a canonical run result, and SHA-256 manifest.
Partial or failed runs publish a failure result and retain only explicitly
classified diagnostic evidence; they cannot be included in the baseline.

The runner has a `--prepare-only` path that validates artifacts and prints the
exact physical command without opening the serial device.
The real path accepts the stable `/dev/serial/by-id/...` device and uses the
same bounded U-Boot transfer or verified MMC artifact identities as the current
physical graphics gate.

## Baseline protocol

Build a current Sv39/SMP=4 kernel and deterministic Stage1, prepare the exact
physical plan, and run three independent physical `profile` boots.
Each run must satisfy all of the following:

- the seven functional groups pass;
- Firefox and Xorg PID/start-time identities remain unchanged;
- the system and thread samplers cover the workload interval;
- the fixture and evidence upload are bound to that run's experiment ID;
- the terminal serial result agrees with the uploaded daily-use result;
- the board returns to the expected U-Boot prompt without a panic or fatal
  device error.

For every run, retain raw input and scroll samples rather than only p50/p95,
navigation command and browser timing separately, all context operation
durations, phase durations, Firefox/Xorg CPU deltas, per-thread runtime,
completed runqueue wait, dispatch deltas, CPU placement, context switches, and
runnable counts.

The analysis reports each run and the median/range across three runs.
A category crossing its diagnostic threshold is a signal, not an automatic
kernel attribution.

## Optimization admission

A mechanism is eligible for optimization only when it recurs in all three
qualified local runs and agrees with the affected phase and primary metric.
The initial classification is:

- **executing:** Firefox main-thread CPU runtime dominates the phase;
- **runnable-delayed:** completed runqueue wait is material relative to runtime;
- **sleeping/blocking:** wall time grows while runtime and runqueue wait do not;
- **mixed or unresolved:** no boundary dominates or evidence is incomplete.

If the mechanism is unresolved, add one bounded diagnostic such as sampled-PC
and repeat the baseline workload before selecting an optimization.
Public-page-only behavior and one-off tails cannot admit a kernel change.

The selected optimization gets one focused failing regression or microbenchmark
before implementation.
The implementation changes one mechanism only; unrelated refactoring and a
second tuning variable are excluded.

## Controlled A/B validation

The unmodified current kernel is variant A and the one-variable kernel is
variant B.
Use the same board, four-hart topology, rootfs, Stage1, DTB, fixture, display
provider, boot arguments, browser package, profile mode, and host runner.
Perform three qualified profile boots for each variant.
When practical, alternate variants to reduce thermal and temporal drift;
otherwise finish with an additional A control to detect drift.

Report raw per-run values and distribution summaries.
A speedup claim requires:

- the primary metric improves in the same direction in all qualified B runs;
- the B median improvement exceeds the complete A baseline range or an
  additional control rules out that overlap;
- functionality remains 7/7 and no secondary category regresses beyond its
  diagnostic threshold;
- the focused regression, host tests, and relevant QEMU tests pass;
- the attribution evidence changes in the direction predicted by the mechanism.

If these conditions are not met, report the result as inconclusive, neutral, or
regressive rather than a speedup.

## Test and review strategy

Host tests cover the fixture upload endpoint, exact experiment binding,
duplicate and malformed uploads, bundle schema and hash validation, atomic host
publication, and failure classification.
Guest tests cover the fixed-action grammar, PID discovery, exact command,
success/failure upload behavior, and prohibition of arbitrary paths and URLs.
The physical runner uses mocked serial and fixture operations to test exact
phase order, timeouts, recovery, and partial evidence.

The Firefox fast check remains the short regression gate.
The current four-hart QEMU smoke is rerun when the host/guest orchestration can
exercise it without pretending that QEMU is physical evidence.
Kernel changes receive focused unit/regression coverage and the relevant
RISC-V QEMU test before the physical B runs.

## Completion criteria

- The host runner and fixed guest action pass review and regression tests.
- Three current-kernel physical profiles form a qualified, auditable baseline.
- Evidence names one repeated mechanism or triggers a bounded attribution
  extension until one mechanism is identified.
- One single-variable optimization has a failing-before/passing-after focused
  test.
- Three qualified optimized physical profiles and the required control runs
  complete under the same contract.
- The final report preserves all hashes and raw results and makes only the
  performance claim supported by the controlled evidence.
