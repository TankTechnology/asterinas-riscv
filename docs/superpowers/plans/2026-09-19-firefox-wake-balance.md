# Waking-task CPU balance implementation plan

Status: executed; reconstructed on 2026-09-20 from the committed change and its
retained evidence. See
[design](../specs/2026-09-19-firefox-wake-balance-design.md) and
[result](../../performance/2026-09-19-firefox-wake-balance-physical-ab.md).

**Goal:** remove the `runnable-delayed` mechanism admitted by the 2026-09-18
physical baseline, changing one scheduler behaviour and nothing else.

This plan is written after the fact. The checkboxes below record what was
actually done, including the steps that did not follow the intended order; they
are not a claim that the sequence was followed at the time.

### Task 1: Focused regressions for CPU selection on wake

**Files:** `kernel/src/sched/sched_class/mod.rs`

- [x] Add `wake_prefers_less_loaded_allowed_cpu`: a waking thread whose
  `last_cpu` is allowed must move off a CPU that already holds a runnable peer.
- [x] Add `wake_preserves_last_cpu_when_load_is_equal`: an equal-load tie must
  stay on `last_cpu`.
- [ ] **Deviation:** both landed in `53c4601a6` together with the
  implementation. The admission rule asks for a failing regression *before*
  implementation; that order was not followed, and neither test was observed
  failing against the unmodified scheduler.

### Task 2: Implement the single variable

**Files:** `kernel/src/sched/sched_class/mod.rs`

- [x] Add `PerCpuLoadStats::runnable_load()` as `queue_len + !is_idle`.
- [x] Probe one rotating alternative CPU per wake through a scheduler-level
  `last_chosen_cpu` cursor instead of scanning every CPU in the hot path.
- [x] Migrate only when `candidate_load < last_load`; keep `last_cpu` on a tie.
- [x] Leave the existing minimum-load scan as the path used when no allowed
  `last_cpu` exists.
- [x] Do not touch affinity, `futex`, the page cache, or any second variable.

### Task 3: Freeze and run three qualified A boots

**Files:** `target/firefox-daily-use-physical/` (generated, untracked)

- [x] Freeze A at `55ee5c64e` with kernel
  `84246481e4eca725c4092939866a9c5a340e8e2896c3f49f8f90ceecca4ceaf8` and
  publish the deployment plan.
- [x] Take three `profile` boots, one per boot, and admit only runs that are
  qualified and recovered: `release-jit-crossarch-run-3/4/5`.

### Task 4: Run three qualified B boots

- [x] Freeze B at `53c4601a6` with kernel
  `6d3fcaa90031f71d26b0bb4471f2ba15407072531e44357737323201ca3424bf`.
- [x] Verify that the B plan differs from the A plan in the kernel artifact
  hash and in no other field.
- [x] Take three `profile` boots: `wake-balance-run-1/2/3`.
- [ ] **Deviation:** the variants were not alternated, and no extra A control
  was taken after the B set to detect drift.

### Task 5: Evaluate against the admission criteria

- [x] Generate both three-run reports with
  `tools/riscv/megrez_firefox_daily_use_report.py`.
- [x] Apply the five controlled-A/B conditions from the optimization design
  verbatim and record which hold.
- [ ] The QEMU half of condition 4 is open: run the RISC-V kernel-test suite
  (`TARGET_ARCH=riscv64 SMP=4 make ktest`) so the two new `#[ktest]`
  regressions execute in QEMU, and record the result here.
- [ ] Publish the outcome as inconclusive/neutral/regressive rather than a
  speedup while condition 4 is open.

### Task 6: Preserve the evidence

**Files:** `docs/performance/2026-09-19-firefox-wake-balance-physical-ab.md`

- [x] Record both variants' identities, per-run values, distribution
  summaries, attribution, and limitations.
- [x] Record that the affected metric and both scroll metrics improve with no
  overlap while two keyboard metrics regress inside the overlapping band.

## Next gate

B classifies as `mixed`, so no further kernel change is eligible. The next step
is one bounded attribution observation against `contextSwitchTotalMs`, which
remains over its 500 ms diagnostic threshold in all three B runs.
