# RISC-V Architecture LTP Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a named, reproducible `arch-riscv64` LTP suite with 138 runnable architecture-sensitive syscall tests, one explicit unavailable test, and SMP=4 as the default gate configuration.

**Architecture:** Keep the existing 767-test syscall suite intact and add a closed suite registry that binds each suite name to a reviewed requested-name manifest and exact selected/unavailable counts. The build selects one suite before packing the initramfs; the runner publishes suite-neutral `manifest.txt` and `unavailable-tests.json` evidence, while `result.json` records the suite name and exact verdict order.

**Tech Stack:** Python 3 `unittest`, Bash, LTP 20260529, C/musl, Asterinas RISC-V Sv39, QEMU `virt`, U-Boot `booti`.

---

## Scope and Plan Boundary

This plan delivers only the named RISC-V architecture gate and its first SMP=4
baseline. It does not change scheduler migration, virtual-memory permissions,
or clone behavior. Those kernel fixes are separate plans and commits so their
regressions remain attributable to one subsystem.

The implementation remains in the isolated worktree on branch
`codex/riscv-ltp-integration`. Generated files below `target/ltp/` remain
untracked evidence.

## File Map

**Create**

- `tools/riscv/ltp/manifests/arch-riscv64.txt` — reviewed requested-name list: 138 runnable entries plus `rt_sigtimedwait01` as the one explicit unavailable entry.
- `tools/riscv/ltp_suite.py` — closed suite definitions, repository-relative manifest paths, and exact count contracts.
- `tools/riscv/ltp/ARCH-RISCV64-M1-report.md` — immutable SMP=4 baseline report written from verified run artifacts.

**Modify**

- `tools/riscv/tests/test_ltp_manifest.py` — tracked architecture-manifest contract tests.
- `tools/riscv/nixos/ltp/build_ltp.sh` — select `syscalls` or `arch-riscv64` before packaging.
- `tools/riscv/tests/test_ltp_guest_runner.py` — build-script suite-selection source contract.
- `tools/riscv/ltp_gate.py` — named-suite CLI, validation, generic evidence names, and SMP=4 default.
- `tools/riscv/tests/test_ltp_gate.py` — suite lookup, dry-run, packaging, status, and evidence tests.
- `tools/riscv/ltp_result.py` — bind normalized results to a suite name.
- `tools/riscv/tests/test_ltp_result.py` — result schema and summary tests.
- `Makefile` — make the LTP target default to SMP=4 and pass a suite name.
- `tools/riscv/ltp/README.md` — architecture-suite build, run, observation, and verification commands.

### Task 1: Publish the Reviewed Architecture Manifest

**Files:**
- Create: `tools/riscv/ltp/manifests/arch-riscv64.txt`
- Modify: `tools/riscv/tests/test_ltp_manifest.py`

- [ ] **Step 1: Write the failing repository-manifest contract**

Add this path and test:

```python
ARCH_RISCV64_MANIFEST = (
    REPO / "tools/riscv/ltp/manifests/arch-riscv64.txt"
)

def test_arch_riscv64_manifest_has_139_unique_requested_names(self) -> None:
    requested = tuple(
        stripped
        for line in ARCH_RISCV64_MANIFEST.read_text().splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    )

    self.assertEqual(len(requested), 139)
    self.assertEqual(len(set(requested)), 139)
    self.assertEqual(requested[0], "brk01")
    self.assertEqual(requested[-1], "membarrier01")
    self.assertTrue(
        {
            "cacheflush01",
            "clone08",
            "getcpu01",
            "mmap04",
            "rt_sigtimedwait01",
            "sched_setaffinity01",
            "futex_waitv03",
        }.issubset(requested)
    )
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
PYTHONPATH=tools/riscv python3 -m unittest \
  tools.riscv.tests.test_ltp_manifest.RepositoryManifestContractTests \
  -v
```

Expected: `FileNotFoundError` for `arch-riscv64.txt`.

- [ ] **Step 3: Create the reviewed requested-name manifest**

Create `tools/riscv/ltp/manifests/arch-riscv64.txt` with one name per line in
this exact order:

```text
brk01
brk02
cacheflush01
clock_gettime01
clock_gettime02
clock_gettime03
clock_gettime04
clone01
clone02
clone03
clone04
clone05
clone06
clone07
clone08
clone09
clone10
clone11
clone301
clone302
clone303
clone304
getcpu01
getcpu02
mmap01
mmap02
mmap03
mmap04
mmap05
mmap06
mmap08
mmap09
mmap12
mmap13
mmap14
mmap15
mmap16
mmap17
mmap18
mmap19
mmap20
mmap21_01
mmap21_02
mmap22
mprotect01
mprotect02
mprotect03
mprotect04
mprotect05
personality01
personality02
prctl01
prctl02
prctl03
prctl05
prctl06
prctl07
prctl08
prctl09
prctl10
ptrace01
ptrace02
ptrace03
ptrace04
ptrace05
ptrace06
ptrace07
ptrace08
ptrace09
ptrace10
ptrace11
rt_sigaction01
rt_sigaction02
rt_sigaction03
rt_sigprocmask01
rt_sigprocmask02
rt_sigqueueinfo01
rt_sigqueueinfo02
rt_sigsuspend01
rt_sigtimedwait01
sched_get_priority_max01
sched_get_priority_max02
sched_get_priority_min01
sched_get_priority_min02
sched_getparam01
sched_getparam03
sched_rr_get_interval01
sched_rr_get_interval02
sched_rr_get_interval03
sched_setparam01
sched_setparam02
sched_setparam03
sched_setparam04
sched_setparam05
sched_getscheduler01
sched_getscheduler02
sched_setscheduler01
sched_setscheduler02
sched_setscheduler03
sched_setscheduler04
sched_yield01
sched_setaffinity01
sched_getaffinity01
sched_setattr01
sched_getattr01
sched_getattr02
set_robust_list01
set_tid_address01
sigaltstack01
sigaltstack02
signal01
signal02
signal03
signal04
signal05
signal06
signalfd01
signalfd02
signalfd4_01
signalfd4_02
uname01
uname02
uname04
futex_cmp_requeue01
futex_cmp_requeue02
futex_wait01
futex_wait02
futex_wait03
futex_wait04
futex_wait05
futex_waitv01
futex_waitv02
futex_waitv03
futex_wake01
futex_wake02
futex_wake03
futex_wake04
futex_wait_bitset01
membarrier01
```

- [ ] **Step 4: Run the manifest contract and verify GREEN**

Run the command from Step 2.

Expected: both repository-manifest contract tests pass.

- [ ] **Step 5: Commit the reviewed manifest**

```bash
git add tools/riscv/ltp/manifests/arch-riscv64.txt \
  tools/riscv/tests/test_ltp_manifest.py
git commit -m "Add reviewed RISC-V architecture LTP manifest"
```

### Task 2: Add a Closed Named-Suite Contract

**Files:**
- Create: `tools/riscv/ltp_suite.py`
- Modify: `tools/riscv/tests/test_ltp_gate.py`

- [ ] **Step 1: Write failing suite lookup tests**

Import `suite_by_name` and `suite_names`, then add:

```python
def test_named_suites_have_closed_count_contracts(self) -> None:
    self.assertEqual(suite_names(), ("syscalls", "arch-riscv64"))

    syscalls = suite_by_name(REPO, "syscalls")
    self.assertEqual(syscalls.expected_selected, 767)
    self.assertEqual(syscalls.expected_unavailable, 12)

    arch = suite_by_name(REPO, "arch-riscv64")
    self.assertEqual(arch.expected_selected, 138)
    self.assertEqual(arch.expected_unavailable, 1)
    self.assertEqual(
        arch.enabled,
        REPO / "tools/riscv/ltp/manifests/arch-riscv64.txt",
    )

    with self.assertRaisesRegex(ValueError, "unknown LTP suite"):
        suite_by_name(REPO, "arbitrary")
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
PYTHONPATH=tools/riscv python3 -m unittest \
  tools.riscv.tests.test_ltp_gate.LtpGatePolicyTests.test_named_suites_have_closed_count_contracts \
  -v
```

Expected: import failure for `ltp_suite`.

- [ ] **Step 3: Implement immutable suite definitions**

Create `tools/riscv/ltp_suite.py`:

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Closed suite definitions for the RISC-V LTP gate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LtpSuite:
    """A reviewed requested manifest and its exact packaging counts."""

    name: str
    enabled: Path
    expected_selected: int
    expected_unavailable: int


_SUITE_SPECS = (
    (
        "syscalls",
        Path("test/initramfs/src/conformance/ltp/testcases/all.txt"),
        767,
        12,
    ),
    (
        "arch-riscv64",
        Path("tools/riscv/ltp/manifests/arch-riscv64.txt"),
        138,
        1,
    ),
)


def suite_names() -> tuple[str, ...]:
    """Returns every accepted suite name in stable CLI order."""

    return tuple(name for name, _, _, _ in _SUITE_SPECS)


def suite_by_name(repo: Path, name: str) -> LtpSuite:
    """Resolves one reviewed suite below the bound repository."""

    for suite_name, relative, selected, unavailable in _SUITE_SPECS:
        if name == suite_name:
            return LtpSuite(
                name=suite_name,
                enabled=repo.resolve() / relative,
                expected_selected=selected,
                expected_unavailable=unavailable,
            )
    raise ValueError(f"unknown LTP suite: {name}")
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the command from Step 2.

Expected: one test passes.

- [ ] **Step 5: Commit the suite contract**

```bash
git add tools/riscv/ltp_suite.py tools/riscv/tests/test_ltp_gate.py
git commit -m "Define named RISC-V LTP suites"
```

### Task 3: Make the LTP Build Select a Named Suite

**Files:**
- Modify: `tools/riscv/nixos/ltp/build_ltp.sh`
- Modify: `tools/riscv/tests/test_ltp_guest_runner.py`

- [ ] **Step 1: Write the failing build-script source contract**

Add:

```python
def test_build_script_selects_only_closed_named_suites(self) -> None:
    source = BUILD_SCRIPT.read_text()

    self.assertIn('SUITE="syscalls"', source)
    self.assertIn('--suite) SUITE="$2"; shift 2 ;;', source)
    self.assertIn('arch-riscv64)', source)
    self.assertIn('EXPECTED_SELECTED=138', source)
    self.assertIn('EXPECTED_UNAVAILABLE=1', source)
    self.assertIn('--expected-count "${EXPECTED_SELECTED}"', source)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
PYTHONPATH=tools/riscv python3 -m unittest \
  tools.riscv.tests.test_ltp_guest_runner.LtpGuestBuildContractTests.test_build_script_selects_only_closed_named_suites \
  -v
```

Expected: failure because the script has no `SUITE` selection.

- [ ] **Step 3: Add closed suite argument parsing and count checks**

Replace the one-argument loop with:

```bash
SUITE="syscalls"
SKIP_COMPILE=0

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --skip-compile) SKIP_COMPILE=1; shift ;;
        --suite) SUITE="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

case "${SUITE}" in
    syscalls)
        ENABLED_TESTS="${REPO_ROOT}/test/initramfs/src/conformance/ltp/testcases/all.txt"
        EXPECTED_SELECTED=767
        EXPECTED_UNAVAILABLE=12
        ;;
    arch-riscv64)
        ENABLED_TESTS="${REPO_ROOT}/tools/riscv/ltp/manifests/arch-riscv64.txt"
        EXPECTED_SELECTED=138
        EXPECTED_UNAVAILABLE=1
        ;;
    *)
        echo "unknown LTP suite: ${SUITE}" >&2
        exit 2
        ;;
esac
```

Use `ENABLED_TESTS` and `EXPECTED_SELECTED` in the selector command. After the
selector, enforce the omission count with:

```bash
ACTUAL_UNAVAILABLE="$(grep -c '"name"' "${UNAVAILABLE}")"
if [[ "${ACTUAL_UNAVAILABLE}" -ne "${EXPECTED_UNAVAILABLE}" ]]; then
    echo "expected ${EXPECTED_UNAVAILABLE} unavailable tests, got ${ACTUAL_UNAVAILABLE}" >&2
    exit 2
fi
```

Print `suite: ${SUITE}` next to the existing manifest count.

- [ ] **Step 4: Run the focused and full host unit tests**

Run:

```bash
PYTHONPATH=tools/riscv python3 -m unittest \
  tools.riscv.tests.test_ltp_guest_runner -v
make test_riscv_ltp_unit
```

Expected: all tests pass.

- [ ] **Step 5: Commit suite-selectable packaging**

```bash
git add tools/riscv/nixos/ltp/build_ltp.sh \
  tools/riscv/tests/test_ltp_guest_runner.py
git commit -m "Build named RISC-V LTP suites"
```

### Task 4: Bind Results to the Named Suite

**Files:**
- Modify: `tools/riscv/ltp_result.py`
- Modify: `tools/riscv/tests/test_ltp_result.py`

- [ ] **Step 1: Write failing schema tests**

Pass `suite="arch-riscv64"` to `build_result_document` and assert:

```python
self.assertEqual(document["schema_version"], 2)
self.assertEqual(document["suite"], "arch-riscv64")
self.assertEqual(
    summary_text(document).splitlines()[0],
    "suite=arch-riscv64 infrastructure=PASS ltp=FAIL",
)
```

Add `--suite arch-riscv64` to the write-CLI fixture and assert the published
JSON has the same suite value.

- [ ] **Step 2: Run result tests and verify RED**

Run:

```bash
PYTHONPATH=tools/riscv python3 -m unittest \
  tools.riscv.tests.test_ltp_result -v
```

Expected: `TypeError` because `build_result_document` does not accept `suite`.

- [ ] **Step 3: Add the suite field to normalized evidence**

Add `suite: str` to `build_result_document`, reject names outside
`suite_names()`, emit schema version 2 and the suite field, and render the first
summary line as:

```python
f"suite={document['suite']} infrastructure={infrastructure} ltp={ltp}\n"
```

Add this required CLI argument:

```python
write.add_argument("--suite", choices=suite_names(), required=True)
```

Pass `args.suite` into `build_result_document`.

- [ ] **Step 4: Run result tests and verify GREEN**

Run the command from Step 2.

Expected: all result tests pass.

- [ ] **Step 5: Commit suite-bound result evidence**

```bash
git add tools/riscv/ltp_result.py tools/riscv/tests/test_ltp_result.py
git commit -m "Record RISC-V LTP suite identity"
```

### Task 5: Generalize the Gate and Default to SMP=4

**Files:**
- Modify: `tools/riscv/ltp_gate.py`
- Modify: `tools/riscv/tests/test_ltp_gate.py`
- Modify: `Makefile`

- [ ] **Step 1: Write failing gate-policy tests**

Change live-status fixtures to write `manifest.txt`. Add a dry-run test using
`--suite arch-riscv64` and assert its output contains:

```text
build_ltp.sh --suite arch-riscv64
ltp_result.py write
--manifest target/ltp/results/arch-dry/manifest.txt
--suite arch-riscv64
--smp 4
```

Add a parser test asserting an omitted `--smp` resolves to 4. Add a runtime
suite-validation fixture where the selected manifest has 138 entries and the
unavailable JSON contains exactly `rt_sigtimedwait01`; mutate either count and
assert `ValueError`.

- [ ] **Step 2: Run gate tests and verify RED**

Run:

```bash
PYTHONPATH=tools/riscv python3 -m unittest \
  tools.riscv.tests.test_ltp_gate -v
```

Expected: failures for the missing `--suite` argument, old evidence filename,
and SMP=1 default.

- [ ] **Step 3: Implement suite-aware orchestration**

Add `--suite` with choices from `suite_names()` and change the run parser's
SMP default to 4. Resolve the suite once at the start of `_run_gate`.

Invoke builds as:

```python
build_command = [str(build_script), "--suite", suite.name]
subprocess.run(build_command, cwd=repo, check=True)
```

Validate both regular and `--skip-build` executions by recomputing
`select_manifest` from the suite's requested manifest, upstream
`target/ltp/src/runtest/syscalls`, and packaged binaries. Require exact
selected lines, `suite.expected_selected`, exact unavailable records, and
`suite.expected_unavailable` before creating QEMU artifacts.

Publish these suite-neutral evidence files in every result directory:

```text
manifest.txt
unavailable-tests.json
```

Make `status` read `manifest.txt`; retain a read-only fallback to
`selected-syscalls` so existing immutable baseline directories remain
inspectable. Pass the suite name and `manifest.txt` to `ltp_result.py`, and add
both new evidence files to `SHA256SUMS`.

Change the Makefile default and invocation to:

```make
SMP ?= 4
RISCV_LTP_SUITE ?= syscalls

python3 tools/riscv/ltp_gate.py run \
    --kernel "$(ASTERINAS_RISCV_BOOTI)" --smp "$(SMP)" \
    --suite "$(RISCV_LTP_SUITE)"
```

- [ ] **Step 4: Run host verification**

Run:

```bash
make test_riscv_ltp_unit
python3 tools/riscv/ltp_gate.py run \
  --kernel target/osdk/aster-kernel-osdk-bin.Image \
  --suite arch-riscv64 --run-id arch-dry --dry-run
git diff --check
```

Expected: unit tests pass; dry-run prints SMP=4, the named suite, and only
run-owned paths; `git diff --check` prints nothing.

- [ ] **Step 5: Commit the named gate**

```bash
git add tools/riscv/ltp_gate.py tools/riscv/tests/test_ltp_gate.py Makefile
git commit -m "Add named RISC-V architecture LTP gate"
```

### Task 6: Document and Run the First SMP=4 Architecture Baseline

**Files:**
- Modify: `tools/riscv/ltp/README.md`
- Create: `tools/riscv/ltp/ARCH-RISCV64-M1-report.md`

- [ ] **Step 1: Add exact operator commands**

Document the named build and gate:

```bash
tools/riscv/nixos/ltp/build_ltp.sh --suite arch-riscv64
python3 tools/riscv/ltp_gate.py run \
  --kernel target/osdk/aster-kernel-osdk-bin.Image \
  --suite arch-riscv64 --smp 4 \
  --run-id arch-riscv64-m1-smp4 --skip-build --baseline \
  --boot-timeout 2400
```

Document concurrent observation commands:

```bash
python3 tools/riscv/ltp_gate.py status --run-id arch-riscv64-m1-smp4
tail -f target/ltp/results/arch-riscv64-m1-smp4/progress.log
```

- [ ] **Step 2: Build the named initramfs in the pinned cross container**

Use the existing pinned image and musl mounts from the README, changing the
inner command to:

```bash
tools/riscv/nixos/ltp/build_ltp.sh --skip-compile --suite arch-riscv64
```

Expected: `suite: arch-riscv64`, `manifest: 138 enabled tests`, and one
unavailable entry named `rt_sigtimedwait01`.

- [ ] **Step 3: Run and actively observe the SMP=4 baseline**

Start the baseline command from Step 1. While it runs, poll `status` and inspect
new `progress.log`/serial output at least once per minute. If progress stops,
inspect the current test, QEMU process, and latest serial output before deciding
whether the run is still healthy.

Expected: infrastructure PASS, exactly 138 ordered verdicts, no kernel panic,
and immutable run-owned artifacts.

- [ ] **Step 4: Verify artifacts and write the baseline report**

Run:

```bash
python3 -m json.tool \
  target/ltp/results/arch-riscv64-m1-smp4/result.json >/dev/null
sha256sum -c \
  target/ltp/results/arch-riscv64-m1-smp4/SHA256SUMS
```

Write `ARCH-RISCV64-M1-report.md` with the source branch and full commit, LTP
tag, suite, SMP count, selected/unavailable counts, normalized verdict counts,
the exact non-pass names, run-owned artifact hashes, result directory, and the
observation record. Derive every number from the verified result files.

- [ ] **Step 5: Run final host checks and commit the evidence report**

```bash
make test_riscv_ltp_unit
git diff --check
git status --short
git add tools/riscv/ltp/README.md \
  tools/riscv/ltp/ARCH-RISCV64-M1-report.md
git commit -m "Record RISC-V architecture LTP baseline"
```

Expected: all host tests pass, formatting is clean, and generated files under
`target/ltp/` remain untracked/ignored.

## Self-Review Record

- Spec coverage: the plan publishes the 139-name requested manifest, selects
  exactly 138 runnable entries, records the one missing binary, removes the
  hard-coded evidence label, binds results to a named suite, defaults to SMP=4,
  actively observes QEMU, and produces an immutable baseline report.
- Scope boundary: scheduler migration, memory-permission reporting, clone ABI,
  expanded `sched`/`nptl`/`mm` builds, and x86 discrimination are intentionally
  separate testable changes.
- Type consistency: `LtpSuite.name`, `enabled`, `expected_selected`, and
  `expected_unavailable` are used consistently by build, gate, result, and
  tests; evidence names are consistently `manifest.txt` and
  `unavailable-tests.json`.
- Placeholder scan: every implementation step names exact files, interfaces,
  commands, expected results, and commit boundaries.
