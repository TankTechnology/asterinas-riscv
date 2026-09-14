# Firefox Interaction Observability M1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic Firefox interaction latency evidence and a stable fbdev display-provider boundary without modifying DRM code.

**Architecture:** A small pure-Python contract validates and summarizes browser timing samples. The existing physical-interaction page records trusted-event-to-frame timings, and the existing gate validates them. The desktop session resolves its Xorg configuration through one fail-closed provider helper; only fbdev is installed now, while a DRM PR can add its own provider directory without changing Firefox or evidence code.

**Tech Stack:** Python 3 standard library, Bash, HTML/JavaScript, `unittest`, Debian rootfs builder, QEMU browser gates.

---

### Task 1: Add the interaction timing contract

**Files:**
- Create: `tools/riscv/debian/rootfs/browser_interaction_perf.py`
- Create: `tools/riscv/tests/test_browser_interaction_perf.py`

- [ ] **Step 1: Write failing contract tests**

```python
class InteractionTimingTests(unittest.TestCase):
    def test_summarizes_bounded_samples_with_nearest_rank_p95(self) -> None:
        result = perf.summarize_input_latencies([12.5, 8.0, 20.0, 10.0])
        self.assertEqual(result["count"], 4)
        self.assertEqual(result["min_ms"], 8.0)
        self.assertEqual(result["p50_ms"], 10.0)
        self.assertEqual(result["p95_ms"], 20.0)
        self.assertEqual(result["max_ms"], 20.0)

    def test_rejects_empty_nonfinite_boolean_and_unbounded_samples(self) -> None:
        for samples in ([], [True], [float("nan")], [0.0], [60_001.0], [1.0] * 65):
            with self.subTest(samples=samples):
                with self.assertRaises(perf.PerformanceContractError):
                    perf.summarize_input_latencies(samples)
```

- [ ] **Step 2: Run the test and observe the missing-module failure**

Run: `python3 -m unittest tools.riscv.tests.test_browser_interaction_perf -v`

Expected: FAIL because `browser_interaction_perf` does not exist.

- [ ] **Step 3: Implement the bounded pure contract**

```python
MAX_INPUT_SAMPLES = 64
MAX_INPUT_LATENCY_MS = 60_000.0

class PerformanceContractError(ValueError):
    """Browser performance evidence violated its bounded schema."""

def _nearest_rank(values: list[float], percentile: int) -> float:
    rank = max(1, math.ceil(percentile * len(values) / 100))
    return values[rank - 1]

def summarize_input_latencies(samples: object) -> dict[str, int | float]:
    if not isinstance(samples, list) or not 1 <= len(samples) <= MAX_INPUT_SAMPLES:
        raise PerformanceContractError("input latency sample count is out of bounds")
    values: list[float] = []
    for sample in samples:
        if isinstance(sample, bool) or not isinstance(sample, (int, float)):
            raise PerformanceContractError("input latency sample is not numeric")
        value = float(sample)
        if not math.isfinite(value) or not 0.0 < value <= MAX_INPUT_LATENCY_MS:
            raise PerformanceContractError("input latency sample is out of bounds")
        values.append(value)
    values.sort()
    return {
        "count": len(values),
        "min_ms": values[0],
        "p50_ms": _nearest_rank(values, 50),
        "p95_ms": _nearest_rank(values, 95),
        "max_ms": values[-1],
    }
```

- [ ] **Step 4: Run the focused test**

Run: `python3 -m unittest tools.riscv.tests.test_browser_interaction_perf -v`

Expected: PASS with both tests successful.

- [ ] **Step 5: Commit the contract**

```bash
git add tools/riscv/debian/rootfs/browser_interaction_perf.py \
  tools/riscv/tests/test_browser_interaction_perf.py
git commit -m "Add browser interaction timing contract"
```

### Task 2: Record trusted input-to-frame samples

**Files:**
- Modify: `tools/riscv/debian/rootfs/physical_graphics_interaction.html`
- Modify: `tools/riscv/debian/rootfs/physical_graphics_gate.py`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Modify: `tools/riscv/tests/test_physical_graphics_gate.py`
- Modify: `tools/riscv/tests/test_debian_browser_web.py`

- [ ] **Step 1: Write failing snapshot tests**

Extend the test fixture snapshot with `inputLatenciesMs: [16.0, 18.5]`, then assert that
`validate_snapshot` returns a summary whose `p95_ms` is `18.5`. Add rejections for an
empty list, more than 64 entries, a boolean, zero, a negative value, NaN, and a value
above 60 seconds.

```python
snapshot = {**self._snapshot(), "inputLatenciesMs": [16.0, 18.5]}
validated = gate.validate_snapshot(
    snapshot, expected_nonce="0123456789abcdef", cycle=1
)
self.assertEqual(validated["inputLatencySummary"]["p95_ms"], 18.5)
```

- [ ] **Step 2: Run the focused test and observe the schema failure**

Run: `python3 -m unittest tools.riscv.tests.test_physical_graphics_gate -v`

Expected: FAIL because the existing exact snapshot schema rejects `inputLatenciesMs`.

- [ ] **Step 3: Add one frame measurement per trusted input event**

Add `inputLatenciesMs` to the page state. On each trusted `input` event, capture
`performance.now()`, mutate the visible state, and use two nested animation frames so
the sample ends after the paint opportunity following the DOM update.

```javascript
function recordInputFrame(startMs) {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const latencyMs = performance.now() - startMs;
    if (Number.isFinite(latencyMs) && latencyMs > 0 && state.inputLatenciesMs.length < 64) {
      state.inputLatenciesMs.push(latencyMs);
      render();
    }
  }));
}

nonceInput.addEventListener("input", (event) => {
  if (event.isTrusted) {
    const startMs = performance.now();
    // Existing sanitization and state mutation remain here.
    render();
    recordInputFrame(startMs);
  }
});
```

- [ ] **Step 4: Validate timings through the shared contract**

Import `summarize_input_latencies` from the installed guest module or repository module,
add `inputLatenciesMs` to `SNAPSHOT_FIELDS`, and return a copied snapshot containing
`inputLatencySummary`. Keep `snapshot_complete` permissive while input is still in
progress, but require at least one valid sample in terminal state.

Install `browser_interaction_perf.py` beside the physical graphics gate as
`/usr/lib/asterinas/browser_interaction_perf.py` and include it in
`browser_web_runtime_digest`, so host tests and guest execution import the same code.

- [ ] **Step 5: Run the physical gate unit suite**

Run: `python3 -m unittest tools.riscv.tests.test_browser_interaction_perf tools.riscv.tests.test_physical_graphics_gate -v`

Expected: PASS.

- [ ] **Step 6: Commit page and gate instrumentation**

```bash
git add tools/riscv/debian/rootfs/physical_graphics_interaction.html \
  tools/riscv/debian/rootfs/physical_graphics_gate.py \
  tools/riscv/debian/rootfs/build_rootfs.sh \
  tools/riscv/tests/test_physical_graphics_gate.py \
  tools/riscv/tests/test_debian_browser_web.py
git commit -m "Measure Firefox trusted input frame latency"
```

### Task 3: Add the display-provider boundary

**Files:**
- Create: `tools/riscv/debian/rootfs/desktop_display_provider.sh`
- Modify: `tools/riscv/debian/rootfs/desktop_m5_session.sh`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Modify: `tools/riscv/tests/test_debian_browser_web.py`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] **Step 1: Write failing provider tests**

Assert that the builder installs `desktop_display_provider.sh`, places the fbdev Xorg
configuration in `/etc/asterinas/display-providers/fbdev/xorg.conf.d`, and includes the
helper in the browser runtime digest. Execute the helper against a temporary provider
root and verify these cases:

```text
fbdev + existing configuration -> prints the exact configuration directory
unset provider -> resolves to fbdev
drm + missing configuration -> exit 65 and no stdout
unknown provider -> exit 64 and no stdout
```

- [ ] **Step 2: Run the rootfs tests and observe the missing-helper failure**

Run: `python3 -m unittest tools.riscv.tests.test_debian_browser_web tools.riscv.tests.test_debian_rootfs -v`

Expected: FAIL because the provider helper and provider-specific directory are absent.

- [ ] **Step 3: Implement a fail-closed provider resolver**

```bash
#!/usr/bin/env bash
set -euo pipefail

readonly provider="${ASTERINAS_DISPLAY_PROVIDER:-fbdev}"
readonly provider_root="${ASTERINAS_DISPLAY_PROVIDER_ROOT:-/etc/asterinas/display-providers}"
case "$provider" in
    fbdev|drm) ;;
    *) exit 64 ;;
esac
readonly config_directory="$provider_root/$provider/xorg.conf.d"
[[ -d "$config_directory" && -f "$config_directory/20-asterinas.conf" ]] || exit 65
printf '%s\n' "$config_directory"
```

- [ ] **Step 4: Route the desktop session through the provider**

Resolve the directory before removing the X11 socket, emit
`display-provider-ready provider=<name>`, and pass
`-configdir "$provider_config_directory"` to both Xorg invocations. Install only the
fbdev configuration. A DRM PR gains a module by installing the DRM directory and setting
`ASTERINAS_DISPLAY_PROVIDER=drm`; it does not need to edit Firefox code.

- [ ] **Step 5: Run the rootfs test suites and shell parser**

Run: `python3 -m unittest tools.riscv.tests.test_debian_browser_web tools.riscv.tests.test_debian_rootfs -v`

Run: `bash -n tools/riscv/debian/rootfs/desktop_display_provider.sh tools/riscv/debian/rootfs/desktop_m5_session.sh tools/riscv/debian/rootfs/build_rootfs.sh`

Expected: both commands PASS.

- [ ] **Step 6: Commit the provider boundary**

```bash
git add tools/riscv/debian/rootfs/desktop_display_provider.sh \
  tools/riscv/debian/rootfs/desktop_m5_session.sh \
  tools/riscv/debian/rootfs/build_rootfs.sh \
  tools/riscv/tests/test_debian_browser_web.py \
  tools/riscv/tests/test_debian_rootfs.py
git commit -m "Add a stable desktop display provider boundary"
```

### Task 4: Publish immutable runtime provenance

**Files:**
- Create: `tools/riscv/debian/rootfs/browser_performance_provenance.py`
- Create: `tools/riscv/tests/test_browser_performance_provenance.py`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Modify: `tools/riscv/debian/rootfs/browser_web_evidence.sh`

- [ ] **Step 1: Write failing provenance tests**

Build a temporary root containing a manifest, Debian status file, fbdev sysfs values,
Xorg binary, Firefox executable, and optional JIT marker. Assert the exact schema:

```python
{
    "schema_version": 1,
    "display_provider": "fbdev",
    "framebuffer": {"width": 1920, "height": 1080, "stride_bytes": 7680, "bits_per_pixel": 32},
    "firefox": {"executable": "/usr/bin/firefox-esr", "jit_overlay": False},
    "packages": {"xserver-xorg-core": "2:21.1.16-1", "xserver-xorg-video-fbdev": "1:0.5.0-2"},
    "rootfs_manifest_sha256": "<64 lowercase hexadecimal characters>"
}
```

Reject missing package records, malformed dimensions, symlinked identity inputs, unknown
providers, and an overlay marker paired with the ESR executable.

- [ ] **Step 2: Run the focused test and observe the missing-module failure**

Run: `python3 -m unittest tools.riscv.tests.test_browser_performance_provenance -v`

Expected: FAIL because the provenance module is absent.

- [ ] **Step 3: Implement bounded file parsing and atomic JSON output**

Use `Path.lstat`, maximum file sizes, exact integer parsing, and SHA-256 streaming. The
module accepts root/proc/sys prefixes for tests, writes JSON to a sibling temporary file,
calls `os.replace`, and never invokes package managers or external network commands.

- [ ] **Step 4: Integrate provenance into browser evidence**

Install the module as `/usr/lib/asterinas/browser-performance-provenance`, add it to
`browser_web_runtime_digest`, run it before the online navigation phase, and include its
path in the final evidence directory. A provenance failure fails the automated gate
before public-network measurements.

- [ ] **Step 5: Run focused and integration unit tests**

Run: `python3 -m unittest tools.riscv.tests.test_browser_performance_provenance tools.riscv.tests.test_debian_browser_web -v`

Expected: PASS.

- [ ] **Step 6: Commit provenance support**

```bash
git add tools/riscv/debian/rootfs/browser_performance_provenance.py \
  tools/riscv/tests/test_browser_performance_provenance.py \
  tools/riscv/debian/rootfs/build_rootfs.sh \
  tools/riscv/debian/rootfs/browser_web_evidence.sh
git commit -m "Record Firefox performance runtime provenance"
```

### Task 5: Gate M1 in the persistent build workflow

**Files:**
- Modify: `tools/riscv/firefox_fast_check.sh`
- Modify: `tools/riscv/debian/rootfs/README.md`
- Modify: `tools/riscv/README.md`

- [ ] **Step 1: Add the new focused suites to the fast check**

Add `test_browser_interaction_perf` and `test_browser_performance_provenance` to the
unittest command, both new Python modules to `py_compile`, and the provider helper to
`bash -n`.

- [ ] **Step 2: Run the fast check**

Run: `tools/riscv/firefox_fast_check.sh`

Expected: `FIREFOX_FAST_CHECK_PASS` and exit status 0.

- [ ] **Step 3: Document the stable fallback and DRM handoff**

Document the default `fbdev` provider, the fail-closed `drm` provider-directory contract,
the provenance artifact, and the distinction between diagnostic measurements and the
100 ms admission target. State that this branch does not implement DRM.

- [ ] **Step 4: Run the browser-web QEMU gate through the persistent container**

Run:

```bash
tools/docker/run_dev_container.sh -- \
  python3 tools/riscv/debian/rootfs/browser_web_qemu_gate.py \
  --help
```

Then run the repository's documented bounded local browser-web QEMU command using the
existing frozen rootfs cache. Expected: the gate reaches its PASS marker, writes timing
and provenance artifacts, and terminates the QEMU process it owns.

- [ ] **Step 5: Confirm the DRM ownership boundary**

Run: `git diff --name-only origin/main...HEAD | rg '^kernel/src/device/drm/|^tools/riscv/drm/'`

Expected: no output and status 1.

- [ ] **Step 6: Commit documentation and gate wiring**

```bash
git add tools/riscv/firefox_fast_check.sh \
  tools/riscv/debian/rootfs/README.md tools/riscv/README.md
git commit -m "Document Firefox interaction performance qualification"
```
