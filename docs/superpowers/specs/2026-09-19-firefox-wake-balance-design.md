# Waking-task CPU balance design

Status: implemented in `53c4601a6`; this design record was written afterwards.
Date: 2026-09-19.
Admission basis: `docs/performance/2026-09-18-firefox-daily-use-physical-baseline.md`.
Result: `docs/performance/2026-09-19-firefox-wake-balance-physical-ab.md`.

## Note on this record's position in the process

The optimization admission rule requires a dated design *before* a kernel
change is selected. The change in `53c4601a6` was implemented and measured
before this file existed; the design was reconstructed on 2026-09-20 from the
admission rule, the baseline that admitted it, and the diff as committed.

Two consequences of that ordering are recorded rather than smoothed over:

- the two `#[ktest]` regressions landed in the same commit as the
  implementation, not as a preceding failing regression, and neither has been
  executed since;
- the four-hart QEMU graphics/control gate was run before the result was read
  and passes, but it does not execute those regressions, so the QEMU evidence
  that exists is not evidence about this change's own tests.

Both are treated as open items in the result record. This file does not
retroactively claim that the process was followed.

## Goal

Remove the mechanism the three-run physical baseline named, without changing
any other scheduling behaviour.

The baseline classified all three qualified Megrez runs as `runnable-delayed`
with 3/3 agreement: the hottest Firefox thread spent 28.4--35.3% of its
runtime plus completed-runqueue-wait in that wait, while Firefox's process CPU
occupancy stayed near 1.1 of 4 CPUs. The mechanism is a bound waking task
landing on a CPU that is not the least-loaded one it is allowed to use.

## Non-goals

- No change to CPU affinity, `futex`, or the page cache.
- No second tuning variable in the same change.
- No load-average or history-based balancing beyond what is needed to act on
  the admitted mechanism.
- No claim about USB-to-HDMI latency, public pages, or display scanout.
- No general `perf_event_open` subsystem and no new sampling profiler.

## Mechanism and hypothesis

`ClassScheduler::select_cpu` returned `last_cpu` whenever the thread's affinity
allowed it, without consulting any runqueue. A task that wakes repeatedly
therefore returns to its previous CPU even when another allowed CPU is idle,
so wakeups do not spread across the four harts.

Hypothesis: if a waking task is placed on a less-loaded allowed CPU, the
completed runqueue wait of the waking thread falls, and the affected primary
metric falls with it. The predicted attribution signature is a lower
`waitRatio` and a higher process CPU occupancy, with no change to the
capability verdicts.

## The single variable

`kernel/src/sched/sched_class/mod.rs`:

- `PerCpuLoadStats::runnable_load()` is added as
  `queue_len.saturating_add(u32::from(!is_idle))`.
- When `last_cpu` is allowed by affinity, `select_cpu` probes one rotating
  alternative CPU rather than scanning every CPU in this hot path. The probe
  cursor is a scheduler-level `last_chosen_cpu`, advanced once per wake.
- It migrates to the candidate only when `candidate_load < last_load`. A tie
  stays on `last_cpu` to retain cache locality.
- The previous behaviour — work through the allowed CPUs and pick the minimum
  load — remains the path taken when there is no allowed `last_cpu`.

The change deliberately probes one alternative per wake instead of evaluating
all CPUs, so the hot path does not pay a scan. The consequence, accepted here,
is that a single wake may compare a loaded candidate against a loaded
`last_cpu` and stay put; the rotation is what eventually samples the idle CPU.

## Fixed inputs

The A and B variants must share the board, the four-hart topology, the rootfs,
Stage1, DTB, fixture, display provider, boot arguments, browser package,
profile mode, and host runner. Only the kernel differs. The A and B runs
recorded in the result document satisfy this: the two kernels are the only
identity field that changes, and both were built from the same
`RELEASE=1`/Sv39/SMP=4 configuration.

## Validation protocol

Three qualified `profile` boots per variant, using the protocol already fixed
by
`docs/superpowers/specs/2026-09-18-firefox-daily-use-physical-optimization-design.md`
("Controlled A/B validation"). That rule is not restated or relaxed here; the
result is judged against it verbatim, including the requirement that a speedup
claim needs *all* of its conditions.

Alternation of variants was not used, and no additional A control was taken
after the B set. Temporal or thermal drift between the two sets is therefore
not excluded by an independent control; the result document states this as a
limitation rather than treating the two sets as paired.

## Completion criteria

The change is complete only when the A/B admission criteria are satisfied in
full, or the result is published as inconclusive, neutral, or regressive. A
non-overlapping improvement in the affected metric alone does not complete it.
