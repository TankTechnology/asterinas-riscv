# Firefox composite performance workload design

## Status and objective

The existing Firefox performance fixture proves that local navigation and a
small synthetic keyboard, pointer, and scroll sequence complete.
It does not apply enough sustained or varied work to identify a dominant
Firefox or kernel performance bottleneck.

This design adds one deterministic, phase-labelled workload that combines
realistic browser use with bounded stress amplification.
The workload must distinguish browser execution, network and file-backed
resource loading, scheduler delay, and memory-management cost without relying
on a public website or rebooting Firefox between phases.

The initial objective is attribution, not a predetermined optimization.
A kernel change is justified only when the same cost class repeats in a
bounded phase and a focused regression or A/B experiment confirms it.

## Current evidence boundary

Two earlier correctness defects are already fixed on `origin/main`:

- file fault-around used byte offsets where page indices were required;
  commit `830c91a68` repairs the exact window and has fault-handler tests;
- RISC-V software-interrupt acknowledgement could erase a newly posted IPI
  after callback draining and strand a remote TLB request;
  commit `269ee6285` acknowledges SSIP before callbacks and includes a bounded
  interleaving model plus a TLB shootdown probe.

Those repairs do not establish that page faults or multi-CPU synchronization
are now cheap in every Firefox workload.
The earlier diagnostic Firefox run observed 31,504 page faults, but its probes
added about 36 percent to startup time and therefore cannot serve as the normal
profiler.
Alternating global and intentionally local-only instruction-cache
synchronization changed median launch time by about 1.3 percent, with
overlapping runs, so global RFENCE was not shown to be the primary bottleneck.
The later schedstat runs reported only 269--326 ms of total Firefox runnable
wait in approximately ten-second windows while Firefox consumed roughly one
CPU continuously.
That evidence argues against runqueue starvation, but it does not measure page
fault latency, TLB shootdown latency, or lock contention directly.

The composite workload will therefore treat page faults and cross-CPU
synchronization as measurable hypotheses rather than solved performance
categories.

## Alternatives considered

### Increase the existing interaction loop

Raising the current eight samples to a larger number is simple, but repeats
the same tiny DOM mutation and two animation frames.
It cannot separate layout, image decode, navigation, cache, or multi-process
costs and is rejected as the primary design.

### Replay public websites

Public pages resemble real browsing, but content, advertisements, DNS, TLS,
network latency, and server behavior vary independently of the kernel.
They remain useful as final acceptance checks, not as attribution workloads.

### Deterministic phase suite

A local fixture can combine representative browser operations with explicit
phase boundaries and fixed resource identities.
Each phase can amplify one cost class while preserving a single Firefox
session and comparable inputs.
This is the selected approach.

## Workload modes

One implementation provides three bounded modes:

| Mode | Intended use | Target duration | Relative work |
| --- | --- | ---: | ---: |
| `smoke` | host tests and QEMU regression | about 15 seconds | 1 |
| `profile` | normal physical attribution | about 60 seconds | 4 |
| `stress` | long-tail and stability checks | 120--180 seconds | 12 |

The mode selects reviewed iteration and resource-count constants.
It does not accept arbitrary page scripts, URLs, durations, or unbounded
concurrency from the command line.

## Phase protocol

The fixture exposes one new exact local path beneath `/browser-quality/`.
The page owns a finite-state machine and accepts one mode before starting.
It publishes a schema-versioned snapshot through Firefox's wrapped page
object.

Every phase has these states:

1. `pending` before any phase operation;
2. `running` with a browser-monotonic start timestamp;
3. `complete` with an end timestamp and bounded metrics;
4. `failed` with one enumerated, redacted reason.

Transitions are append-only and ordered.
The capture program rejects missing, duplicate, reordered, oversized, or
unknown phases.
Host-monotonic command boundaries and browser-monotonic measurements remain
separate clock domains.

### Warm-up

Warm-up loads the page, creates the fixed DOM skeleton, performs a small
resource request, and waits for two animation frames.
It is recorded but excluded from scored phase summaries.

### Interaction and layout

This phase performs deterministic text edits, pointer-target updates, scroll
steps, and repeated style/layout reads and writes over a fixed large DOM.
It records event-to-first-frame, event-to-next-frame, phase duration, long
frames, and completed operation counts.
Synthetic events remain labelled synthetic; they are not described as
physical USB-to-HDMI latency.

### Canvas and image decode

This phase decodes a fixed set of local PNG resources and draws them with
deterministic canvas transforms.
Resource bytes, dimensions, decode count, and draw count are fixed per mode.
The phase reports decode and draw completion separately so software rendering
does not get confused with network transfer.

### Concurrent resources

This phase fetches fixed HTML, JSON, binary, and image resources with bounded
concurrency and cache-busting sequence identifiers understood by the local
fixture.
It performs a cold/no-store pass followed by a cache-eligible pass.
The fixture records request start, completion, response bytes, status, and
observed concurrency without recording secret URLs or arbitrary headers.

### Navigation and history

This phase performs repeated navigation among exact local pages and bounded
history back/forward operations.
It records command duration and valid portions of Navigation Timing.
A known negative Firefox `fetchStart` continues to fail strict navigation
validation; it is retained as an issue and must not be clamped to zero.
Other independently valid phase evidence is still published.

### Multi-context activity

This phase opens at most three controlled browsing contexts, starts fixed
background resource and timer work, switches the selected context, and closes
the additional contexts.
It exercises Firefox process/thread wakeups and IPC without creating an
unbounded tab or process storm.
Cleanup is part of the phase contract and is verified before completion.

### Cooldown

Cooldown stops all timers and pending fixture work, waits for two animation
frames, verifies that only the original context remains, and publishes the
terminal snapshot.
Firefox and Xorg stay alive for later experiments.

## System evidence

The capture program samples system evidence concurrently with the phase suite.
It retains the existing exact Firefox and Xorg process identities and extends
the ledger with phase markers.

Required low-overhead evidence is:

- per-process user and kernel CPU ticks;
- per-thread CPU runtime, completed runnable wait, and dispatch count from
  Linux-compatible `schedstat`;
- global and per-CPU busy fractions, runnable count, and context switches;
- process RSS and available system memory when the compatible procfs fields
  exist;
- process minor and major fault counters when the compatible procfs fields
  exist;
- fixture request counts, bytes, status, and concurrency;
- browser phase duration, frame latency, navigation intervals, and operation
  counts.

Unsupported fields are named explicitly.
The collector must not invent zero values for absent procfs interfaces.
Detailed syscall, fault, and serial probes remain opt-in because previous
measurements showed substantial perturbation.

The first version does not claim direct lock-contention or TLB-shootdown
timing unless a dedicated compatible counter is implemented and qualified.
Instead, a phase that shows high kernel CPU or fault growth becomes the input
to a focused, short diagnostic experiment.

## Evidence schema and publication

The capture publishes one private JSON report and an early checkpoint.
The report includes:

- schema version, workload version, selected mode, and exact fixture origin;
- Firefox and Xorg PID/start-time identities;
- ordered phase results and their clock domains;
- aligned system-sample intervals and phase labels;
- fixture summary and truncation status;
- explicit supported and unsupported observation fields;
- completion, cleanup, and validation status.

The interaction/phase checkpoint is written before strict navigation
validation so a navigation anomaly cannot erase completed evidence.
Files use exclusive creation, mode `0600`, bounded JSON depth and size, and
atomic final publication where the existing publisher already provides it.

## Failure and recovery behavior

The workload fails closed on an unknown origin, unexpected page, invalid mode,
phase reordering, resource mismatch, process identity change, output collision,
or deadline expiry.
It reports bounded enumerated state rather than arbitrary page text or URLs.

A failed workload stops its own timers and extra contexts but does not reboot
the board, restart Firefox, rewrite the Debian partition, or delete the
existing profile.
Board recovery remains under the established physical-session policy.

## Testing strategy

Development follows test-driven increments:

1. fixture tests require exact new resources, cache policy, request accounting,
   and bounded concurrency;
2. browser contract tests reject malformed phase states, metrics, ordering,
   bounds, and clock-domain confusion;
3. capture tests require checkpoint survival, PID identity stability,
   cleanup, private output, and no session deletion or reboot;
4. system-ledger tests cover optional memory/fault fields without silently
   changing the existing schedstat ABI;
5. QEMU runs the `smoke` mode and requires all phases and cleanup;
6. one physical boot runs repeated `profile` samples with identical artifacts;
7. only a repeated attributed cost advances to a focused kernel regression
   and controlled A/B optimization.

Public Baidu browsing and human keyboard/pointer checks remain separate final
acceptance evidence.

## Acceptance criteria

The implementation is ready for physical attribution when:

- all five scored phases complete in `smoke` and `profile` modes;
- output proves exact mode, phase order, counts, resource identities, and
  Firefox/Xorg identities;
- each profile phase has aligned browser and system intervals;
- cleanup leaves the original Firefox session usable;
- the workload is deterministic and fully local;
- focused host tests and the RISC-V QEMU smoke gate pass;
- documentation clearly separates synthetic frame latency, browser navigation,
  process CPU, page faults, scheduler wait, and physical display latency.

No fivefold speedup, page-fault elimination, or SMP optimization is an
acceptance criterion for the measurement work itself.
Those are downstream claims requiring their own before/after evidence.
