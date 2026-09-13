# Firefox dmesg Correlation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Current collaboration constraints prohibit subagent dispatch. Preserve the dirty worktree, make no commits, and do not mutate remote PRs.

**Goal:** Produce one bounded RISC-V QEMU Firefox experiment that correlates an unmodified dmesg stream with Marionette, Firefox actor, process-lifecycle, and per-thread syscall evidence to identify the first missing execution boundary.

**Architecture:** `aster-logger` gains a default-preserving capture threshold independent of console output. A maintained host experiment runner injects a bounded guest-side dmesg follower into the existing checkpoint experiment, validates and exports its output, collects actor records, and generates an evidence classification. Short unit and micro-guest gates precede the single long Firefox run.

**Tech Stack:** Safe Rust, Asterinas component/cmdline APIs, C regression probes, util-linux dmesg 2.41.5, Python 3 standard library, existing physical-graphics QEMU gate, persistent Docker caches.

---

## File map

- Modify `kernel/comps/logger/Cargo.toml`: add direct `aster-cmdline` and `spin` dependencies.
- Modify `kernel/comps/logger/src/klog/store.rs`: pure capture-level parser and effective-filter helper.
- Modify `kernel/comps/logger/src/klog/store_tests.rs`: standalone parser/filter tests.
- Modify `kernel/comps/logger/src/aster_logger.rs`: register `asterinas.klog_capture` and separate capture from console threshold.
- Modify `book/src/kernel/linux-compatibility/kernel-parameters.md`: document the new parameter and defaults.
- Modify `test/initramfs/src/regression/process/syslog/syslog.c`: assert that an opt-in informational lifecycle record is retained.
- Modify `tools/riscv/diagnostics/klog_dmesg_probe.c`: exercise `dmesg --follow-new --raw`.
- Modify `tools/riscv/diagnostics/klog_micro_gate.py` and its tests: boot with capture/syscall diagnostics and validate retained-but-not-printed info.
- Create `tools/riscv/diagnostics/firefox_dmesg_records.py`: frame validation, dmesg parsing, timeline and classification.
- Create `tools/riscv/tests/test_firefox_dmesg_records.py`: pure parser/classifier regressions.
- Create `tools/riscv/diagnostics/firefox_dmesg_experiment.py`: maintained bounded experiment runner.
- Create `tools/riscv/tests/test_firefox_dmesg_experiment.py`: source transformation and resource-bound tests.
- Create a new immutable `target/firefox-dmesg-20260908/` artifact tree during verification.
- Create `docs/porting/evidence/2026-09-08-firefox-dmesg-correlation.md`: commands, hashes, results and supported claim.

## Task 1: Independent capture threshold

**Files:**
- Modify: `kernel/comps/logger/src/klog/store.rs`
- Modify: `kernel/comps/logger/src/klog/store_tests.rs`
- Modify: `kernel/comps/logger/src/aster_logger.rs`
- Modify: `kernel/comps/logger/Cargo.toml`
- Modify: `book/src/kernel/linux-compatibility/kernel-parameters.md`

- [x] **Step 1: Add failing parser/filter tests**

Add these standalone behaviors to `store_tests.rs`:

```rust
#[test]
fn capture_level_accepts_linux_spelling_and_rejects_invalid_values() {
    for (text, expected) in [("0", 0), ("8", 8), ("off", 0),
        ("warn", 5), ("warning", 5), ("info", 7), ("debug", 8)] {
        assert_eq!(store::parse_level_filter(text), Some(expected));
    }
    for text in ["", "9", "Info", "6x", "-1"] {
        assert_eq!(store::parse_level_filter(text), None);
    }
}

#[test]
fn capture_threshold_never_reduces_console_threshold() {
    assert_eq!(store::effective_level_filter(0, None), 5);
    assert_eq!(store::effective_level_filter(0, Some(7)), 7);
    assert_eq!(store::effective_level_filter(8, Some(5)), 8);
}
```

- [x] **Step 2: Verify RED**

Run through the persistent container:

```sh
tools/docker/run_dev_container.sh --workspace "$KLOG_WORKTREE" --offline -- \
    make test_klog_store TARGET_ARCH=riscv64
```

Expected: compile failure because `parse_level_filter` and `effective_level_filter` do not exist.

- [x] **Step 3: Implement the pure helpers**

In `store.rs`, define a named default and strict parser:

```rust
pub(super) const DEFAULT_CAPTURE_LEVEL_FILTER: u8 = 5;

pub(super) fn parse_level_filter(value: &str) -> Option<u8> {
    match value {
        "0" | "off" => Some(0),
        "1" | "emerg" => Some(1),
        "2" | "alert" => Some(2),
        "3" | "crit" => Some(3),
        "4" | "error" | "err" => Some(4),
        "5" | "warning" | "warn" => Some(5),
        "6" | "notice" => Some(6),
        "7" | "info" => Some(7),
        "8" | "debug" => Some(8),
        _ => None,
    }
}

pub(super) fn effective_level_filter(console: u8, capture: Option<u8>) -> u8 {
    console.max(capture.unwrap_or(DEFAULT_CAPTURE_LEVEL_FILTER))
}
```

- [x] **Step 4: Verify GREEN**

Run `make test_klog_store TARGET_ARCH=riscv64` and require 14 passed, zero failed.

- [x] **Step 5: Wire the command-line parameter**

Add direct workspace dependencies and define storage in `aster_logger.rs`:

```rust
use alloc::string::String;
use spin::Once;

static CAPTURE_LEVEL: Once<String> = Once::new();
aster_cmdline::define_kv_param!("asterinas.klog_capture", CAPTURE_LEVEL);
```

During logger initialization, preserve `boot_level` as the console policy.
Parse `CAPTURE_LEVEL`; warn once on an invalid value; compute the effective filter
using the pure helper and call `ostd::log::set_max_level(LevelFilter::from_u8(...))`.
Do not alter runtime syslog console controls.

- [x] **Step 6: Document the parameter**

Add an Asterinas-specific section describing values `0..8`, lowercase aliases,
default `warning`, independence from `loglevel`, last-value behavior,
and the Firefox diagnostic example.

- [x] **Step 7: Format and compile**

Run scoped rustfmt, 14 store tests, and the offline incremental RISC-V kernel build:

```sh
make -o initramfs kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
```

Record the elapsed build and distinguish pre-existing warnings from new warnings.

## Task 2: Prove retained-but-quiet informational records

**Files:**
- Modify: `test/initramfs/src/regression/process/syslog/syslog.c`
- Modify: `tools/riscv/diagnostics/klog_dmesg_probe.c`
- Modify: `tools/riscv/diagnostics/klog_micro_gate.py`
- Modify: `tools/riscv/tests/test_klog_micro_gate.py`

- [x] **Step 1: Add a failing lifecycle-retention C case**

Open a nonblocking `/dev/kmsg` reader at `SEEK_END`, fork a child that exits,
and wait for it. Search at most 256 records for a kernel-facility priority-6 record
containing `syscall_diag lifecycle=clone` for the parent PID.
The test must fail against the prior kernel booted with `loglevel=off`
because its effective capture threshold is warning.

- [x] **Step 2: Change the follow probe to the exact target mode**

Use:

```c
execl("/dmesg", "dmesg", "--follow-new", "--raw", NULL);
```

Retain the existing two acknowledged markers, monotonic five-second deadline,
read/byte counts, child-state report and targeted cleanup.

- [x] **Step 3: Add failing gate assertions**

Extend the Python fixture transcript and tests so the gate requires:

```text
test_capture_level_retains_info summary: ... 0 tests failed
KLOG_CAPTURE_INFO_RETAINED=1
```

The lifecycle text must be absent from the console-probe phase
and present only after `KLOG_DMESG_BEGIN` in the final dmesg dump.
Require each dmesg-follow stage to complete with no old-buffer drain checkpoint.

- [x] **Step 4: Verify RED and then update the micro boot arguments**

First run parser tests and observe the missing marker failure.
Then make `klog_micro_gate.py` append exactly:

```text
loglevel=off asterinas.klog_capture=info asterinas.syscall_diag=1 console=ttyS0
```

- [x] **Step 5: Rebuild only the C probes and micro initramfs**

Use the existing cached Nix RISC-V compiler and existing initramfs stage.
Compile with `-O2 -Wall -Wextra -Werror`; do not install a toolchain.

- [x] **Step 6: Run the short guest gate**

Boot the newly built kernel with SMP4 and require:

- all C assertions pass;
- BusyBox and util-linux dmesg exit zero;
- two `--follow-new` markers arrive;
- informational lifecycle record is retained but absent from console phase;
- QEMU exits zero with no panic/fatal marker.

Freeze kernel and initramfs copies plus SHA-256 values under the new artifact tree.

## Task 3: Dmesg frame and classification library

**Files:**
- Create: `tools/riscv/diagnostics/firefox_dmesg_records.py`
- Create: `tools/riscv/tests/test_firefox_dmesg_records.py`

- [x] **Step 1: Write failing frame receiver tests**

Specify an `A_FF_DMESG` protocol with a metadata frame followed by indexed payload frames:

```text
A_FF_DMESG_META {"version":1,"raw_bytes":123,"retained_bytes":123,
 "discarded_bytes":0,"discarded_lines":0,"early_exit":false,"returncode":-15}
A_FF_DMESG part=0/1 sha256=<64 hex> data=<base64>
```

Tests require canonical integer/boolean types, one metadata record,
contiguous indices, a 4 MiB decoded bound, valid base64/zlib/SHA-256,
no trailing compressed stream, and refusal of duplicate or incomplete frames.

- [x] **Step 2: Verify RED**

Run:

```sh
python3 -m unittest tools/riscv/tests/test_firefox_dmesg_records.py
```

Expected: import failure because the module does not exist.

- [x] **Step 3: Implement `DmesgFrames`**

Expose:

```python
class DmesgFrames:
    def accept(self, line: str) -> bytes | None: ...
    def finish(self) -> None: ...
```

`finish()` must fail unless metadata and one complete payload were observed.
It must reject `discarded_bytes != 0`, `discarded_lines != 0`, `early_exit=true`,
or a return code other than the documented targeted shutdown values.

- [x] **Step 4: Write failing parsing/timeline tests**

Use realistic color-free raw lines such as:

```text
<6>[  510.852000] syscall_diag lifecycle=wait pid=70 tid=70 ... result=-10 outcome=error ...
<14>[  510.853000] ASTERINAS_FF_KLOG version=1 phase=request_enter request_id=4 firefox_pid=70 client_pid=252
```

Test strict marker keys, finite timestamps, request/PID positivity,
lifecycle extraction, stable actor request identity,
and preservation of host receipt time as a separate clock domain.

- [x] **Step 5: Implement classifier rules**

Expose:

```python
def correlate(dmesg: bytes, actor_records: list[dict], snapshots: list[dict],
              transport_records: list[dict]) -> dict: ...
```

Return observations, first missing boundary, selected subsystem or `None`,
evidence completeness, and explicit claim limits.
Never classify an isolated syscall wait as a missed wakeup.

- [x] **Step 6: Verify GREEN and malformed cases**

Run the complete records test file and require zero failures.

## Task 4: Maintained bounded experiment runner

**Files:**
- Create: `tools/riscv/diagnostics/firefox_dmesg_experiment.py`
- Create: `tools/riscv/tests/test_firefox_dmesg_experiment.py`

- [x] **Step 1: Write failing source-transformation tests**

Pin the SHA-256 of the frozen wide checkpoint runner.
Tests require exact single anchors for:

- guest imports;
- command selection;
- before/during/after capture calls;
- selected command invocation;
- guest main/finally boundary.

Reject a changed hash, missing/duplicate anchor, an output directory containing files,
or a generated serial command over 4000 bytes.

- [x] **Step 2: Write failing bounded collector tests**

Execute the generated guest collector class natively with a fake child that emits
partial lines, more than 4 MiB, exits early, or ignores SIGTERM.
Assert continuous drain, exact discarded counts, finite cleanup,
targeted PID handling, and one metadata/payload sequence.

- [x] **Step 3: Implement the runner**

Load the frozen wide checkpoint runner by verified hash.
Generate a guest source that:

```python
collector = BoundedDmesg(limit=4 * 1024 * 1024)
collector.start(["/usr/bin/dmesg", "--follow-new", "--raw"])
try:
    result = module["main"]()
finally:
    collector.mark("collector_stopping", ...)
    collector.stop_and_emit()
```

Write `/dev/kmsg` markers around every specified phase.
Set the selected `ExecuteScript` transport budget to exactly 300 seconds.
Enable `ASTERINAS_FIREFOX_ACTOR_DIAGNOSTICS=1` and add
`asterinas.klog_capture=info asterinas.syscall_diag=1` without changing `loglevel=off`.

On the host, collect dmesg frames with an independent serial cursor,
collect actor records using `ActorRecords`, retain all parser errors,
and decode snapshots with their existing receiver.
Always write `experiment-progress.json` after packaging, boot, selected-request,
each snapshot, dmesg export and classification so interruption does not erase prior stages.

- [x] **Step 4: Implement immutable results**

Refuse to overwrite a nonempty output directory.
Write decoded `dmesg.raw`, `dmesg-meta.json`, `actor-stages.jsonl`,
`timeline.json`, `classification.json`, `result.json`, full serial log,
and an input/source hash manifest.
Mark `physical=false` and `browser_acceptance=false` in every summary.

- [x] **Step 5: Verify GREEN**

Run the experiment tests plus existing actor, checkpoint, transport,
rootfs-overlay and QEMU contract tests selected by exact filenames.
Do not start the long Firefox guest yet.

## Task 5: Freeze inputs and run one experiment

**Files/artifacts:**
- Create: `target/firefox-dmesg-20260908/actor-payload/`
- Create: `target/firefox-dmesg-20260908/browser-rootfs-actor/`
- Create: `target/firefox-dmesg-20260908/run-01/`
- Create: `target/firefox-dmesg-20260908/actor-payload-v2/`
- Create: `target/firefox-dmesg-20260908/browser-rootfs-actor-v2/`
- Create: `target/firefox-dmesg-20260908/run-02/`

- [x] **Step 1: Rebuild the actor archive from the pinned source**

Run `transform_archive` using the current reviewed helper.
Assert the source archive hash, allowlisted members, deterministic ZIP output,
and the wrapper's exact profile-permission anchor.

- [x] **Step 2: Materialize a new immutable root image**

Use `dev_overlay.materialize_rootfs` against
`target/firefox-diagnostics-20260908/browser-rootfs-final`.
Do not modify that base or reuse the earlier actor output.
Record image, manifest, package lock and modified-member hashes.

- [x] **Step 3: Preflight resources and extrapolate**

Confirm no QEMU process is running for this worktree,
the persistent container is healthy, required artifacts are nonempty,
and available memory/disk cover one 2 GiB QEMU plus an immutable root-image run copy.
Estimate the run at 10–18 minutes from prior cold-start data;
do not launch if the output gate or short tests are not green.

- [x] **Step 4: Run exactly one complete diagnostic guest**

Invoke the maintained runner with the frozen final kernel, new actor root image,
existing U-Boot/DTB/stage1 inputs, SMP4 and existing page/profile configuration.
Use one outer timeout that covers boot, setup, the fixed 300-second selected call,
three snapshots, dmesg export and clean QEMU shutdown.

- [x] **Step 5: Validate before interpreting**

Require source/input hashes, dmesg metadata and payload,
three snapshot digests, actor/transport parser state,
stable Firefox identity, full 300-second request budget,
and no fatal transcript after terminal output.
If any fails, classify `evidence_incomplete` and stop without a kernel hypothesis.

## Task 6: Analyze and record the first broken boundary

**Files:**
- Create: `docs/porting/evidence/2026-09-08-firefox-dmesg-correlation.md`
- Update: `docs/superpowers/plans/2026-09-08-firefox-dmesg-correlation.md`

- [x] **Step 1: Generate and independently recalculate the classification**

Run `correlate` from retained artifacts in a fresh process and compare its canonical JSON
with the complete `run-02/classification.json`.  `run-01` remains immutable as
an incomplete collector-ordering attempt and is not used for the conclusion.
Do not edit immutable run artifacts to make the comparison pass.

- [x] **Step 2: Trace the selected boundary into source**

Follow the request ID, Firefox/target PID, current syscall and lifecycle record
backward through the exact Firefox and Asterinas call paths.
State observed facts separately from inference.

- [x] **Step 3: Choose only one next microtest**

If the evidence selects one subsystem, write the smallest Linux-referenced microtest proposal
for that syscall/object relationship.
Do not implement a semantic fix in this plan.
If no subsystem is selected, identify the first incomplete boundary rather than adding broad logs.

- [x] **Step 4: Write the evidence note**

Record all commands, durations, hashes, completeness limits,
the first broken boundary, competing hypotheses eliminated,
remaining hypotheses, observer effects and the single next test.
State explicitly that this is QEMU evidence, not physical acceptance.

- [x] **Step 5: Fresh final verification**

Run scoped format/whitespace checks, all new unit tests,
store tests, source hash checks, artifact classification replay,
and read the entire serial transcript for fatal markers after the apparent result.
Only then claim the boundary was identified.
