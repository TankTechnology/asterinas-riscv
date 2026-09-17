# Firefox Composite Workload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, phase-labelled Firefox workload with smoke/profile/stress modes and aligned low-overhead browser, process-fault, memory, CPU, and schedstat evidence.

**Architecture:** Add a dedicated workload page and resource endpoints to the existing local fixture, validate its append-only phase protocol in a small Python contract module, and drive it through a separate Marionette capture command so the legacy latency gate remains stable. Extend the existing procfs sampler with Linux-shaped minor/major-fault and RSS counters, then package the new collectors into the development overlay and qualify the fast path before any physical run.

**Tech Stack:** Python 3 standard library and `unittest`, deterministic HTML/JavaScript, Firefox Marionette WebDriver commands, Linux-compatible procfs, persistent Asterinas Docker/QEMU tooling.

---

## File map

- Create `tools/riscv/debian/rootfs/browser_workload_contract.py`: mode constants and strict workload snapshot validation.
- Create `tools/riscv/debian/rootfs/browser_composite_capture.py`: one-session Marionette driver, checkpoints, phase observations, and concurrent system sampler.
- Create `tools/riscv/tests/test_browser_workload_contract.py`: malformed/valid protocol tests.
- Create `tools/riscv/tests/test_browser_composite_capture.py`: URL, polling, checkpoint, identity, and CLI tests.
- Modify `tools/riscv/megrez_network_fixture.py`: deterministic workload HTML, resource endpoint, and bounded workload request accounting.
- Modify `tools/riscv/tests/test_megrez_network_fixture.py`: exact resources, mode constants, query rejection, and request-summary tests.
- Modify `tools/riscv/debian/rootfs/browser_system_time.py`: process minor/major faults, RSS, and optional MemAvailable evidence.
- Modify `tools/riscv/tests/test_browser_system_time.py`: proc stat/meminfo parsing and interval-delta tests.
- Modify `tools/riscv/debian/rootfs/browser_web_dev_overlay.json`: install the two new guest tools.
- Modify `tools/riscv/tests/test_debian_rootfs.py`: require exact overlay source and destination entries.
- Modify `tools/riscv/firefox_fast_check.sh`: run the new focused suites and compile the new modules.
- Modify `tools/riscv/debian/rootfs/README.md`: document modes, evidence boundary, commands, and interpretation.

### Task 1: Define the workload protocol

**Files:**
- Create: `tools/riscv/debian/rootfs/browser_workload_contract.py`
- Create: `tools/riscv/tests/test_browser_workload_contract.py`

- [ ] **Step 1: Write the failing valid-snapshot test**

```python
from tools.riscv.debian.rootfs.browser_workload_contract import (
    PHASES,
    validate_workload_snapshot,
)

def test_accepts_complete_ordered_smoke_snapshot(self) -> None:
    snapshot = complete_snapshot("smoke")
    report = validate_workload_snapshot(snapshot, expected_mode="smoke")
    self.assertEqual(tuple(phase["name"] for phase in report["phases"]), PHASES)
    self.assertEqual(report["state"], "complete")
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_browser_workload_contract.BrowserWorkloadContractTests.test_accepts_complete_ordered_smoke_snapshot -v
```

Expected: import failure because `browser_workload_contract.py` does not exist.

- [ ] **Step 3: Implement constants and strict validation**

The public surface is exactly:

```python
MODES = {
    "smoke": {"scale": 1, "deadline_seconds": 30},
    "profile": {"scale": 4, "deadline_seconds": 120},
    "stress": {"scale": 12, "deadline_seconds": 300},
}
PHASES = (
    "warmup",
    "interaction-layout",
    "canvas-image",
    "concurrent-resources",
    "navigation-history",
    "multi-context",
    "cooldown",
)

class WorkloadContractError(ValueError):
    pass
```

Define `validate_workload_snapshot(value: object, *, expected_mode: str,
allow_running: bool = False) -> dict[str, object]` beside these constants.

Validation requires schema version 1, workload version 1, clock domain
`browser-performance-now`, the exact expected mode, state `running`,
`complete`, or `failed`, the exact phase prefix while running and all phases
when terminal, finite ordered timestamps in `[0, 3_600_000]`, enumerated phase
states, bounded integer operation/request/context counts, frame samples in
`[0, 60_000]`, and an error of `None` or a 96-character ASCII token.
Return a new normalized dictionary rather than mutating the input.

- [ ] **Step 4: Add malformed-protocol tests**

Add individual tests that reject reordered phases, duplicate phases, an
unknown mode, completion with a missing cooldown, booleans used as integers,
negative or non-finite timing, more than 256 frame samples, oversized counts,
and arbitrary error text.

- [ ] **Step 5: Run the focused contract suite and verify GREEN**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_browser_workload_contract -v
```

Expected: all workload contract tests pass.

- [ ] **Step 6: Commit the protocol**

```bash
git add tools/riscv/debian/rootfs/browser_workload_contract.py \
        tools/riscv/tests/test_browser_workload_contract.py
git commit -m "Define Firefox composite workload protocol"
```

### Task 2: Serve a deterministic phased workload

**Files:**
- Modify: `tools/riscv/megrez_network_fixture.py`
- Modify: `tools/riscv/tests/test_megrez_network_fixture.py`

- [ ] **Step 1: Write failing exact-resource tests**

Require these constants and responses:

```python
BROWSER_WORKLOAD_PATH = "/browser-quality/workload.html"
BROWSER_WORKLOAD_RESOURCE_PATH = "/browser-quality/workload-resource.bin"
BROWSER_WORKLOAD_IMAGE_PATH = "/browser-quality/workload-image.png"
WORKLOAD_RESOURCE_SIZE = 64 * 1024
```

The HTML must expose `__asterinasStartCompositeWorkload` and
`__asterinasCompositeWorkloadSnapshot`, contain every phase name, and have
JavaScript that passes `node --check` after extraction.
The binary and image endpoints accept only canonical
`mode=(smoke|profile|stress)&phase=(image|resource|context)&sequence=N&pass=(cold|warm)`
queries with `0 <= N < 256`; duplicate, missing, unknown, or reordered
parameters return 400.

- [ ] **Step 2: Run fixture tests and verify RED**

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_network_fixture.MegrezNetworkFixtureTests.test_serves_composite_workload_and_bounded_resources -v
```

Expected: import failure for `BROWSER_WORKLOAD_PATH`.

- [ ] **Step 3: Add workload request accounting**

Add a separate `MAX_WORKLOAD_REQUEST_RECORDS = 512` ledger with records:

```python
{
    "monotonic_start_ns": start_ns,
    "monotonic_end_ns": end_ns,
    "mode": mode,
    "phase": phase,
    "sequence": sequence,
    "pass": pass_name,
    "status": status,
    "body_bytes": len(body),
    "active_at_start": active,
}
```

Track current and maximum active requests under the existing fixture lock.
Expose them from `summary()` as `workload_request_count`,
`workload_records_truncated`, `workload_max_active`, and
`workload_requests`. Do not mix these records into the legacy payload gate.

- [ ] **Step 4: Implement the workload page**

The page uses reviewed per-mode constants:

```javascript
const modes = {
  smoke:  {scale: 1, nodes: 128, resources: 8, contexts: 2},
  profile:{scale: 4, nodes: 512, resources: 32, contexts: 3},
  stress: {scale:12, nodes: 1024, resources: 96, contexts: 3}
};
```

It implements one async function per phase, appends phase state before work,
records `startMs`/`endMs`, counts operations, and always runs bounded cleanup.
Interaction/layout mutates and reads a fixed DOM and captures two-rAF samples;
canvas/image decodes local images and draws deterministic transforms;
concurrent-resources uses a worker pool capped at eight; navigation/history
loads exact local pages in one iframe; multi-context creates at most three
iframes, runs bounded fetch/timer work, then removes them; cooldown clears
timers, contexts, and waits for two frames.

The start function accepts a mode only once and catches errors into an ASCII
enumerated token. Snapshot uses `JSON.parse(JSON.stringify(state))` so callers
cannot mutate live state.

- [ ] **Step 5: Run all fixture tests and verify GREEN**

```bash
python3 -m unittest tools.riscv.tests.test_megrez_network_fixture -v
```

Expected: all tests pass and the JavaScript syntax check succeeds when Node is
available.

- [ ] **Step 6: Commit the fixture workload**

```bash
git add tools/riscv/megrez_network_fixture.py \
        tools/riscv/tests/test_megrez_network_fixture.py
git commit -m "Add deterministic Firefox composite workload"
```

### Task 3: Record process faults, RSS, and available memory

**Files:**
- Modify: `tools/riscv/debian/rootfs/browser_system_time.py`
- Modify: `tools/riscv/tests/test_browser_system_time.py`

- [ ] **Step 1: Write failing procfs parser tests**

Extend the test stat builder with `minflt`, `majflt`, and `rss_pages`, then
require:

```python
self.assertEqual(parsed.minor_faults, 12)
self.assertEqual(parsed.major_faults, 3)
self.assertEqual(parsed.rss_pages, 400)
self.assertEqual(parse_mem_available("MemTotal: 1 kB\nMemAvailable: 700 kB\n"), 700)
```

Reject counter overflow, counter regression, negative RSS, duplicate
MemAvailable, wrong units, and malformed optional meminfo.

- [ ] **Step 2: Run the parser tests and verify RED**

```bash
python3 -m unittest \
  tools.riscv.tests.test_browser_system_time.ProcParserTests -v
```

Expected: missing `ProcessCpu` fields and `parse_mem_available`.

- [ ] **Step 3: Extend process and snapshot types**

Add these immutable fields:

```python
@dataclass(frozen=True)
class ProcessCpu:
    pid: int
    comm: str
    minor_faults: int
    major_faults: int
    utime_ticks: int
    stime_ticks: int
    starttime_ticks: int
    rss_pages: int

@dataclass(frozen=True)
class Snapshot:
    guest_monotonic_ns: int
    system: SystemCpu
    processes: tuple[ProcessCpu, ...]
    memory_available_kib: int | None = None
```

Parse Linux `/proc/<pid>/stat` fields 10, 12, 14, 15, 22, and 24 using the
existing post-comm field indexing. Read `/proc/meminfo` only when present;
FileNotFound marks memory unavailable, while malformed present content fails.

- [ ] **Step 4: Report validated deltas**

Each process interval adds `minor_faults`, `major_faults`, and
`rss_pages_after`. The system section adds
`memory_available_kib_before/after` when both snapshots support it. Remove
these names from `unsupported`; keep absent memory explicit in unsupported.

- [ ] **Step 5: Run the complete sampler suite and verify GREEN**

```bash
python3 -m unittest tools.riscv.tests.test_browser_system_time -v
```

Expected: all existing CPU/schedstat tests and new fault/memory tests pass.

- [ ] **Step 6: Commit procfs evidence**

```bash
git add tools/riscv/debian/rootfs/browser_system_time.py \
        tools/riscv/tests/test_browser_system_time.py
git commit -m "Record browser process fault and memory evidence"
```

### Task 4: Drive and checkpoint the composite workload

**Files:**
- Create: `tools/riscv/debian/rootfs/browser_composite_capture.py`
- Create: `tools/riscv/tests/test_browser_composite_capture.py`

- [ ] **Step 1: Write failing URL and completed-capture tests**

Require:

```python
self.assertEqual(
    workload_url("http://10.0.2.2:17894/browser-quality/index.html"),
    "http://10.0.2.2:17894/browser-quality/workload.html",
)
report = capture_composite(fake, BASE, mode="smoke", timeout_seconds=30)
self.assertEqual(report["workload"]["state"], "complete")
self.assertEqual(len(report["phase_observations"]), 7)
```

Reject non-fixture URLs, invalid modes, a skipped phase, terminal failure,
poll output above 256 KiB, a deadline, and a changed document.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
python3 -m unittest tools.riscv.tests.test_browser_composite_capture -v
```

Expected: import failure because the capture module does not exist.

- [ ] **Step 3: Implement browser capture**

Reuse `_navigate`, `_wait_document`, `_script`, `_json_value`, `_private_json`,
and `_process_starttimes` from `browser_perf_capture.py`.
Navigate to the exact workload page, call:

```javascript
if (document.URL !== arguments[0] ||
    !window.wrappedJSObject.__asterinasStartCompositeWorkload) return 'missing';
window.wrappedJSObject.__asterinasStartCompositeWorkload(arguments[1]);
return 'started';
```

Poll the snapshot at 50 ms in smoke mode and 100 ms otherwise.
Validate every value with `allow_running=True`; whenever the completed phase
prefix grows, append `{"phase": name, "observed_guest_monotonic_ns": now}` and
invoke the checkpoint callback. Return only after strict terminal validation.

- [ ] **Step 4: Implement one-session publication and CLI**

`run_composite_capture` creates a verified Marionette session, writes one
guest-monotonic ready marker, starts `browser_system_time.run_sampler` and
`run_thread_sampler` in separate threads, and writes these exclusive private
artifacts:

- `browser-composite-checkpoint.json` after each phase via atomic replace of a
  private temporary file;
- `browser-system-time.json` from the sampler;
- `browser-thread-time.json` from the Firefox thread/schedstat sampler;
- `browser-composite-capture.json` only after terminal validation and stable
  Firefox/Xorg start times.

Increase the qualified thread-sampler interval bound to 64 and use schedules
`(0.5, 30)`, `(1.0, 64)`, and `(2.5, 64)` for both samplers in smoke, profile,
and stress. The CLI requires exact Firefox/Xorg PIDs, mode, local fixture URL,
evidence directory, and a mode-bounded timeout. It accepts an explicit
`--physical` provenance flag, closes only the transport socket, and never
sends `DeleteSession` or reboots.

- [ ] **Step 5: Verify checkpoint and identity failure tests**

Tests must prove a completed prefix survives later phase failure, an existing
artifact is rejected, output mode is `0600`, the CPU sampler error propagates,
PID reuse fails publication, either sampler failure propagates, and the fake session never receives
`WebDriver:DeleteSession`.

- [ ] **Step 6: Run capture suites and verify GREEN**

```bash
python3 -m unittest \
  tools.riscv.tests.test_browser_workload_contract \
  tools.riscv.tests.test_browser_composite_capture \
  tools.riscv.tests.test_browser_perf_capture -v
```

Expected: all new and legacy capture tests pass.

- [ ] **Step 7: Commit the capture driver**

```bash
git add tools/riscv/debian/rootfs/browser_composite_capture.py \
        tools/riscv/tests/test_browser_composite_capture.py
git commit -m "Capture phased Firefox workload evidence"
```

### Task 5: Package the tools and update the fast gate

**Files:**
- Modify: `tools/riscv/debian/rootfs/browser_web_dev_overlay.json`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`
- Modify: `tools/riscv/firefox_fast_check.sh`

- [ ] **Step 1: Write failing overlay tests**

Require source hashes and destination paths for:

```text
usr/lib/asterinas/browser_workload_contract.py
usr/lib/asterinas/browser_composite_capture.py
```

Also require both paths in the deterministic Stage1 development payload test.

- [ ] **Step 2: Run focused overlay tests and verify RED**

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_rootfs.DebianRootfsTests.test_browser_web_development_overlay_is_source_pinned -v
```

Expected: missing overlay destinations.

- [ ] **Step 3: Add pinned overlay entries**

Add both regular files with mode `0755`, owner/group zero, their exact source
paths, and SHA-256 digests generated by the existing overlay update mechanism.
Do not rebuild the Debian base image.

- [ ] **Step 4: Extend the fast gate**

Add the two new unittest modules and Python source files to
`firefox_fast_check.sh`. Preserve the existing suite order and fail-fast shell
behavior.

- [ ] **Step 5: Run overlay and fast checks**

```bash
python3 -m unittest tools.riscv.tests.test_debian_rootfs -v
tools/riscv/firefox_fast_check.sh
```

Expected: overlay tests and all Firefox fast checks pass without downloads.

- [ ] **Step 6: Commit packaging**

```bash
git add tools/riscv/debian/rootfs/browser_web_dev_overlay.json \
        tools/riscv/tests/test_debian_rootfs.py \
        tools/riscv/firefox_fast_check.sh
git commit -m "Package Firefox composite workload tools"
```

### Task 6: Document and qualify the smoke path

**Files:**
- Modify: `tools/riscv/debian/rootfs/README.md`
- Create: `docs/performance/2026-09-17-firefox-composite-workload.md`

- [ ] **Step 1: Document exact commands and evidence limits**

Document the three modes and a command of this form:

```bash
python3 /run/asterinas-tools/browser_composite_capture.py \
  --firefox-pid "$FIREFOX_PID" --xorg-pid "$XORG_PID" \
  --mode smoke --timeout-seconds 45 \
  --fixture-index-url http://10.0.2.2:17894/browser-quality/index.html \
  --evidence-dir /run/asterinas-browser-composite
```

State that phase timings, procfs faults, schedstat waits, and HDMI latency are
different quantities; absent counters are unsupported, not zero; Baidu is an
acceptance check, not attribution input.

- [ ] **Step 2: Run host verification**

```bash
tools/riscv/firefox_fast_check.sh
python3 -m compileall -q \
  tools/riscv/debian/rootfs/browser_workload_contract.py \
  tools/riscv/debian/rootfs/browser_composite_capture.py \
  tools/riscv/debian/rootfs/browser_system_time.py
git diff --check
```

Expected: all commands exit zero.

- [ ] **Step 3: Build/reuse the development overlay and run RISC-V QEMU smoke**

Use the persistent container and existing cached rootfs artifacts:

```bash
tools/docker/run_dev_container.sh --workspace "$PWD" -- \
  tools/riscv/debian/rootfs/build_rootfs.sh --profile browser-web-dev-overlay
```

Run the registered four-hart browser-web QEMU gate with the existing fixture
and select composite `smoke`; save serial output and the three JSON artifacts
under `target/firefox-composite-workload/qemu-smoke/`. Success requires all
seven ordered phases, stable process identities, sampler completion, cleanup,
and no panic. If the cached graphical image is unavailable, record that exact
infrastructure boundary and do not substitute a host browser result.

- [ ] **Step 4: Record measured qualification**

Write exact commits, artifact hashes, commands, test counts, phase durations,
fault deltas, CPU/schedstat summaries, and unsupported fields in
`docs/performance/2026-09-17-firefox-composite-workload.md`. Do not claim an
optimization or bottleneck from the smoke run.

- [ ] **Step 5: Commit documentation and qualification**

```bash
git add tools/riscv/debian/rootfs/README.md \
        docs/performance/2026-09-17-firefox-composite-workload.md
git commit -m "Qualify Firefox composite workload"
```

### Task 7: Prepare the physical profile experiment

**Files:**
- No source changes. Publish runtime artifacts beneath
  `target/current-main-physical-graphics/physical/`.

- [ ] **Step 1: Reuse one stable physical boot**

Deploy no partition update when the development overlay already contains the
new scripts. Start the fixture, verify exact kernel/Stage1/rootfs identities,
and keep Firefox/Xorg alive for all samples.

- [ ] **Step 2: Run three profile captures**

Run mode `profile` three times in the same boot with separate exclusive output
directories. Between runs verify Firefox/Xorg start times and that only the
original browsing context remains.

- [ ] **Step 3: Compare phase-aligned evidence**

For each phase report wall duration, Firefox user/kernel CPU, minor/major
faults, RSS, total and main-thread runnable wait, dispatch count, global and
per-core busy fractions, and fixture concurrency. A candidate bottleneck must
repeat in at least two of three runs and exceed measurement resolution.

- [ ] **Step 4: Stop before changing the kernel**

Select one repeated cost class for a focused regression or A/B experiment.
Do not change VM, locking, scheduler, TLB, or page-cache behavior in the same
commit as the measurement workload.

## Execution decision

The approved user request is to implement now. The repository is already in a
dedicated `codex/firefox-performance-m2` worktree. Multi-agent delegation was
not requested, so execute this plan inline with `superpowers:executing-plans`
and checkpoint after the host-qualified implementation before any physical
board mutation.
