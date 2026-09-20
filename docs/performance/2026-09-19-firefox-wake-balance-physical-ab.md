# Waking-task CPU balance, physical A/B, 2026-09-19

## Scope and status

This record evaluates the single kernel variable introduced by `53c4601a6`
against the `runnable-delayed` mechanism that the 2026-09-18 physical baseline
admitted. It is a physical Megrez measurement, not a QEMU result.

**All five admission conditions in
`docs/superpowers/specs/2026-09-18-firefox-daily-use-physical-optimization-design.md`
hold, under the reading set out in "Verdict" below**, so an affected-metric
speedup is admitted. The admission is narrow and must be read with the
qualifications that section states: it covers `contextSwitchTotalMs` and the two
scroll metrics, which improve with no overlap between the variants, while six of
the nine primary metrics overlap and two keyboard metrics move the other way
inside that overlap. Two process limitations remain and are recorded there
rather than waived.

Both variants are fully qualified: six runs, all `qualified=true`,
`recovered=true`, all seven functional groups passing.

## Variants

| | Commit | Kernel SHA-256 | Report |
| --- | --- | --- | --- |
| A (baseline) | `55ee5c64e24981ff10a9f76c77b5ae1f875a1eae` | `84246481e4eca725c4092939866a9c5a340e8e2896c3f49f8f90ceecca4ceaf8` | `target/firefox-daily-use-physical/release-jit-baseline-report.{json,md}` |
| B (one variable) | `53c4601a602fe11c426de38909914960c44aa890` | `6d3fcaa90031f71d26b0bb4471f2ba15407072531e44357737323201ca3424bf` | `target/firefox-daily-use-physical/wake-balance-report.{json,md}` |

Run directories: `release-jit-crossarch-run-3/4/5` (A) and
`wake-balance-run-1/2/3` (B).

### The variants differ by exactly one field

Comparing the two frozen deployment plans,
`plan-release-jit-crossarch-55ee5c64.json` and
`plan-release-jit-wake-balance-53c4601a.json`, the only differing field in the
entire plan is the kernel artifact hash. Boot arguments, Stage1, DTB, root
image, rootfs manifest, package lock, package checksums, `InRelease`, SMP count,
Sv39 selection, and fixture identity are identical.

`A` and `B` therefore share one immutable deployment identity except for the
kernel. This is the strongest available statement that the comparison isolates
one variable.

## Per-run primary metrics (ms)

| Metric | A-3 | A-4 | A-5 | B-1 | B-2 | B-3 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| keyboardFirstRafP95Ms | 23 | 22 | 73 | 55 | 38 | 68 |
| keyboardNextRafP95Ms | 41 | 53 | 77 | 60 | 42 | 80 |
| pointerFirstRafP95Ms | 98 | 122 | 78 | 56 | 117 | 47 |
| pointerNextRafP95Ms | 147 | 130 | 78 | 63 | 122 | 66 |
| scrollFirstRafP95Ms | 62 | 174 | 74 | 58 | 14 | 56 |
| scrollNextRafP95Ms | 72 | 182 | 78 | 65 | 27 | 64 |
| navigationCommandMs | 116.504 | unsupported | 137.799 | 102.625 | 95.913 | 168.155 |
| navigationResponseToDomMs | 241 | unsupported | 204 | 190 | 201 | 312 |
| contextSwitchTotalMs | 951.791 | 1256.545 | 1215.464 | 882.499 | 880.612 | 890.514 |

### Distribution summaries

| Metric | A median | A range | B median | B range | Non-overlapping improvement |
| --- | ---: | --- | ---: | --- | --- |
| keyboardFirstRafP95Ms | 23 | 22–73 | 55 | 38–68 | no |
| keyboardNextRafP95Ms | 53 | 41–77 | 60 | 42–80 | no |
| pointerFirstRafP95Ms | 98 | 78–122 | 56 | 47–117 | no |
| pointerNextRafP95Ms | 130 | 78–147 | 66 | 63–122 | no |
| scrollFirstRafP95Ms | 74 | 62–174 | 56 | 14–58 | **yes** |
| scrollNextRafP95Ms | 78 | 72–182 | 64 | 27–65 | **yes** |
| navigationCommandMs | unsupported (2/3) | — | 102.625 | 95.913–168.155 | not comparable |
| navigationResponseToDomMs | unsupported (2/3) | — | 201 | 190–312 | no |
| contextSwitchTotalMs | 1215.464 | 951.791–1256.545 | 882.499 | 880.612–890.514 | **yes** |

Three of the nine metrics improve with no overlap at all: both scroll metrics
and the context-switch total. The two keyboard metrics move the other way
within the overlapping band.

## Attribution

| | Wait ratio | Main runtime share | CPU occupancy | Unaccounted | Classification |
| --- | --- | --- | --- | --- | --- |
| A | 0.3531 / 0.2842 / 0.3065 | 0.4446 / 0.4762 / 0.4620 | 1.0926 / 1.1397 / 1.1785 | 0.0 | `runnable-delayed` 3/3 |
| B | 0.1018 / 0.1128 / 0.1377 | 0.4591 / 0.4579 / 0.4496 | 1.3628 / 1.3427 / 1.3860 | 0.0 | `mixed` 3/3 |

The admitted mechanism is gone: completed runqueue wait fell from 28.4–35.3%
of runtime-plus-wait to 10.2–13.8%, and process CPU occupancy rose from about
1.09–1.18 to about 1.34–1.39, which is the direction the hypothesis predicted.
`leaderHottestInEveryRun` is true in both sets.

No boundary is now dominant. B classifies as `mixed` because `waitRatio` is
below the 0.20 `runnable-delayed` gate and `mainRuntimeShare` (0.4496–0.4591)
stops just short of the 0.50 `executing` gate; `unaccounted` is 0.0, so
`sleeping-blocking` is excluded.

## Admission criteria, evaluated one by one

1. **Primary metric improves in the same direction in all qualified B runs.**
   The design does not name a single primary metric. The report's affected
   metric (`affectedPrimaryMs`, the maximum over the primary set) is
   `contextSwitchTotalMs` in all six runs; on that reading the condition holds,
   and it also holds for both scroll metrics. On an all-nine-metrics reading it
   fails, because six metrics have overlapping distributions.
2. **B median improvement exceeds the complete A range.** Holds for
   `contextSwitchTotalMs` (882.499 < 951.791) and for both scroll metrics
   (56 < 62 and 64 < 72). Fails for the other six.
3. **Functionality remains 7/7 and no secondary category regresses beyond its
   diagnostic threshold.** Holds. All six runs are 7/7, and no category that
   was inside its threshold in A crosses in B: `input` crossings fall from 2/3
   to 1/3, `scroll` from 1/3 to 0/3, `context-switch` stays at 3/3.
4. **The focused regression, host tests, and relevant QEMU tests pass.**
   **Satisfied.** A RISC-V kernel-test run at `53c4601a6`, taken in a separate
   worktree so the deployed kernel artifact was not replaced, executed both new
   regressions and both report `ok`:

   ```text
   test aster_kernel::sched::sched_class::tests::wake_prefers_less_loaded_allowed_cpu ... ok
   test aster_kernel::sched::sched_class::tests::wake_preserves_last_cpu_when_load_is_equal ... ok
   ```

   The same suite at A (`55ee5c64e`) has an identical failure set:
   `aster_kernel::device::tty::tests::tty_echo_runs_without_the_line_discipline_lock`,
   `aster_kernel::syscall::clock_gettime::tests::reports_tick_resolution_for_cpu_clocks`,
   `xarray::test::no_leakage`, and `xarray::test::remove_shrinks_empty_nodes`.
   Those four therefore pre-date the change and are not attributable to it. The
   only difference between the two runs is that B passes two more
   `aster_kernel` tests, 242 against 240, and those two are exactly the new
   regressions.

   `tools/riscv/firefox_fast_check.sh` passes (487 tests,
   `FIREFOX_FAST_CHECK_PASS`). The four-hart QEMU graphics/control gate passes
   with retained evidence: `qemu-qualification`, `qemu-qualification-current`,
   `qemu-smoke-fixed5`, and `qemu-namespace` each report `passed=true`,
   `reason=pass`, `physical=false`, and three interaction cycles.
   `qemu-qualification` records Stage1
   `0d4ebedf76d920c00d2168097b632e06b9e9ba77bed0113cea7d3eb54a6b9ed2`, which is
   the digest the two-build determinism check produced.
5. **The attribution evidence changes in the direction predicted by the
   mechanism.** Holds, as tabulated above.

## Verdict

All five conditions now hold, under the reading that "the primary metric" is
the metric the classifier names as affected. That is the better supported
reading: the report computes `affectedPrimaryMs` as the maximum over its
primary set, that maximum is `contextSwitchTotalMs` in all six runs, and the
design speaks of one primary metric while the report presents nine.

Under that reading the design permits a speedup claim for
`contextSwitchTotalMs` and for both scroll metrics: every B value lies below
every A value, and each B median lies below the complete A range. It does not
permit one for the other six metrics, and the two keyboard metrics moved in the
opposite direction inside the overlapping band. A published claim must carry
both halves, and the removed-mechanism result is the stronger of the two.

Two process limitations remain, recorded rather than waived. They do not void
any of the five conditions, which are conditions on outcomes:

- the focused regressions landed with the implementation instead of preceding
  it, so neither was ever observed failing against the unmodified scheduler;
- the variants were not alternated and no additional A control was taken after
  the B set, so thermal and temporal drift between the two sets is not excluded
  by an independent control.

Recorded result: **the admitted mechanism is removed, and an affected-metric
speedup is admitted with mixed aggregate metrics stated alongside.**

### Correction, 2026-09-20

An earlier revision of this document stated that the fourteen
`target/firefox-daily-use-physical/qemu-*` directories were all empty and that
the QEMU gate had therefore retained no evidence. That was wrong. The gate's
output directories are created root-owned mode `0700` inside the development
container, so a host-side `find`/`ls` run as an unprivileged user cannot read
them; suppressing stderr made the permission failures look like empty
directories. Every one of the fourteen contains files, and four of them record
a passing gate with three interaction cycles.

Read this gate's evidence from inside the container, or as root, and do not
treat an unreadable directory as an empty one.

The practical consequence for the next step follows from the classification
rather than from the metric table. B is `mixed`, so the optimization admission
rule applies: do not select another kernel change. The next kernel-side action
is one bounded attribution observation (sampled PC is eligible only if
instruction-region attribution is the missing boundary), with its overhead
measured, before any further tuning.

`contextSwitchTotalMs` remains above its 500 ms diagnostic threshold in all
three B runs and is still the largest primary metric, so it is the natural
target for that attribution.

## Limitations

- Variants were not alternated and no additional A control was taken after the
  B set, so thermal or temporal drift between the two sets is not excluded.
- Browser timings are not USB-to-HDMI latency; display scanout is unsupported.
- The mechanism class is an admission rule, not proof of a specific function or
  subsystem.
- Navigation aggregates require three supported runs and have two in A, so the
  navigation metrics are not comparable across the variants.
- The two keyboards metrics regressed within the overlapping band; they stay
  under the 100 ms input threshold in both variants.
- Six runs were taken on one board; this is a single-board result.
