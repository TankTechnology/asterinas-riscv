# RISC-V LTP Gate and 767-Test Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an isolated, reproducible RISC-V QEMU LTP gate and capture an evidence-backed 767-test SMP=1 baseline without importing kernel fixes from `origin/track/nixos`.

**Architecture:** Transplant the proven musl LTP guest runner and build recipe, but execute them through registered QEMU profiles and the repository's immutable artifact/session pipeline. LTP owns `target/ltp/qemu/` and timestamped result directories; host parsing separates infrastructure success from LTP verdicts and normalizes the historical aggregate-failure count.

**Tech Stack:** Rust/Asterinas RISC-V Sv39 kernel, LTP 20260529, C/musl, Bash, Python 3 `unittest`, QEMU `virt`, U-Boot `booti`, Docker.

---

## Scope and Plan Boundary

This is the first of three implementation plans derived from
`docs/superpowers/specs/2026-08-20-riscv-ltp-layered-integration-design.md`.
It delivers the gate and baseline only. It deliberately does not transplant
point fixes or the loop-device subsystem. Those plans are written after this
one produces the current branch's real failure list.

The auxiliary DRM track is also outside this plan and remains on a separate
branch; no DRM file or remote integration commit may enter the LTP range.

The implementation starts in an isolated worktree on branch
`codex/riscv-ltp-integration`, based on commit `c91ae3ca4` or its reviewed
descendant. Preserve every untracked file in the original worktree.

## File Map

**Create**

- `tools/riscv/ltp_result.py` — parse and validate guest verdicts; write normalized JSON/text results.
- `tools/riscv/ltp_manifest.py` — select enabled LTP entries and report missing binaries without silent omission.
- `tools/riscv/ltp_gate.py` — orchestrate build, private preparation, guarded QEMU execution, parsing, hashes, and exit policy.
- `tools/riscv/nixos/ltp/build_ltp.sh` — cross-build and package LTP 20260529.
- `tools/riscv/nixos/ltp/init_ltp.c` — PID 1 for the LTP initramfs.
- `tools/riscv/nixos/ltp/ltp_runner.c` — per-test watchdog and serial verdict emitter.
- `tools/riscv/nixos/ltp/etc-passwd` — musl NSS fixture.
- `tools/riscv/nixos/ltp/etc-group` — musl NSS fixture.
- `tools/riscv/tests/test_ltp_result.py` — result-parser contract tests.
- `tools/riscv/tests/test_ltp_manifest.py` — manifest selection and missing-binary tests.
- `tools/riscv/tests/test_ltp_gate.py` — CLI, path isolation, and exit-policy tests.
- `tools/riscv/tests/test_ltp_guest_runner.py` — host-compiled guest-runner classification tests.
- `tools/riscv/ltp/README.md` — reproducible build/run instructions and evidence schema.
- `tools/riscv/ltp/BASELINE-M1-report.md` — generated only after the real run.

**Modify**

- `tools/riscv/qemu_uboot_profiles.py` — register fixed SMP=1 and SMP=4 LTP profiles.
- `tools/riscv/prepare_qemu_uboot_booti.sh` — allow only the additional fixed output root `target/ltp/qemu`.
- `tools/riscv/tests/test_qemu_uboot_contracts.py` — verify the two registered LTP profiles.
- `tools/riscv/tests/test_qemu_uboot_booti.py` — verify canonical LTP output paths and unchanged generic paths.
- `test/initramfs/src/conformance/ltp/testcases/all.txt` — restore the reviewed M27 expanded manifest.
- `Makefile` — add unit and runtime LTP targets.
- `tools/riscv/README.md` — link the LTP gate documentation.

## Remote Provenance

Use final file contents from these commits, not a merge of the remote branch:

- Guest/build base: `be13b75a8`.
- Expanded manifest: `a4ef18017`.
- Historical tooling lineage for review only: `3662d1254`, `4ccf07554`,
  `f88a75414`, `ebda2cfdc`, `61edb1bca`.

Record these hashes in commit messages and the baseline report.

### Task 1: Normalize LTP Serial Results

**Files:**
- Create: `tools/riscv/ltp_result.py`
- Create: `tools/riscv/tests/test_ltp_result.py`

- [ ] **Step 1: Write the failing parser tests**

Create tests containing a historical-format summary and exact verdict lines:

```python
SERIAL = """\
[PASS] read01
[FAIL] open01
[CONF] bind04
[CRASH] connect01
[TIMEOUT] fcntl14
__LTP_GATE_DONE__
[summary] total=5 pass=1 fail=3 conf=1 crash=1 timeout=1
__LTP_GATE_FAIL__
"""

def test_parse_normalizes_aggregate_failures(self) -> None:
    result = parse_ltp_serial(SERIAL)
    self.assertEqual(result.counts.total, 5)
    self.assertEqual(result.counts.pass_count, 1)
    self.assertEqual(result.counts.fail_count, 1)
    self.assertEqual(result.counts.conf_count, 1)
    self.assertEqual(result.counts.crash_count, 1)
    self.assertEqual(result.counts.timeout_count, 1)
    self.assertEqual(result.counts.legacy_fail_total, 3)
    self.assertFalse(result.ltp_passed)

def test_parse_rejects_double_counted_or_missing_verdicts(self) -> None:
    bad = SERIAL.replace("total=5", "total=6")
    with self.assertRaisesRegex(ValueError, "summary total"):
        parse_ltp_serial(bad)

def test_parse_rejects_duplicate_terminal_markers(self) -> None:
    with self.assertRaisesRegex(ValueError, "DONE marker"):
        parse_ltp_serial(SERIAL + "__LTP_GATE_DONE__\n")
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
PYTHONPATH=tools/riscv python3 -m unittest \
  tools.riscv.tests.test_ltp_result -v
```

Expected: `ERROR` with `ModuleNotFoundError: No module named 'ltp_result'`.

- [ ] **Step 3: Implement the parser and immutable value types**

Implement these public interfaces:

```python
VERDICT_RE = re.compile(r"^\[(PASS|FAIL|CONF|CRASH|TIMEOUT)\] ([^\s]+)$")
SUMMARY_RE = re.compile(
    r"^\[summary\] total=(\d+) pass=(\d+) fail=(\d+) "
    r"conf=(\d+) crash=(\d+) timeout=(\d+)$"
)

@dataclass(frozen=True)
class LtpVerdict:
    name: str
    verdict: str

@dataclass(frozen=True)
class LtpCounts:
    total: int
    pass_count: int
    fail_count: int
    conf_count: int
    crash_count: int
    timeout_count: int
    legacy_fail_total: int

@dataclass(frozen=True)
class ParsedLtpResult:
    counts: LtpCounts
    verdicts: tuple[LtpVerdict, ...]
    ltp_passed: bool

def parse_ltp_serial(serial_text: str) -> ParsedLtpResult:
    lines = serial_text.replace("\r", "").splitlines()
    if lines.count("__LTP_GATE_DONE__") != 1:
        raise ValueError("expected exactly one LTP DONE marker")
    terminal_count = (
        lines.count("__LTP_GATE_PASS__") + lines.count("__LTP_GATE_FAIL__")
    )
    if terminal_count != 1:
        raise ValueError("expected exactly one LTP PASS/FAIL marker")
    summaries = [match for line in lines if (match := SUMMARY_RE.fullmatch(line))]
    if len(summaries) != 1:
        raise ValueError("expected exactly one LTP summary")
    values = tuple(int(value) for value in summaries[0].groups())
    total, passed, aggregate_fail, conf, crash, timeout = values
    plain_fail = aggregate_fail - crash - timeout
    if plain_fail < 0:
        raise ValueError("aggregate fail is smaller than crash + timeout")
    if total != passed + plain_fail + conf + crash + timeout:
        raise ValueError("summary total is inconsistent")
    verdicts = tuple(
        LtpVerdict(match.group(2), match.group(1))
        for line in lines
        if (match := VERDICT_RE.fullmatch(line))
    )
    observed = Counter(item.verdict for item in verdicts)
    expected = {
        "PASS": passed,
        "FAIL": plain_fail,
        "CONF": conf,
        "CRASH": crash,
        "TIMEOUT": timeout,
    }
    if len(verdicts) != total or any(observed[name] != count for name, count in expected.items()):
        raise ValueError("verdict lines do not match summary")
    if len({item.name for item in verdicts}) != len(verdicts):
        raise ValueError("duplicate LTP verdict name")
    ltp_passed = aggregate_fail == 0
    if ltp_passed != ("__LTP_GATE_PASS__" in lines):
        raise ValueError("terminal marker disagrees with summary")
    return ParsedLtpResult(
        counts=LtpCounts(total, passed, plain_fail, conf, crash, timeout, aggregate_fail),
        verdicts=verdicts,
        ltp_passed=ltp_passed,
    )
```

Add `to_json_dict()`, `summary_text()`, and a CLI `write` subcommand that reads
`serial.log`, merges `git_commit`, `profile`, `smp`, `infrastructure_passed`,
and artifact hashes, then writes `result.json` and `summary.txt` using a
temporary file plus `os.replace`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Expected: all parser tests pass.

- [ ] **Step 5: Commit the parser**

```bash
git add tools/riscv/ltp_result.py tools/riscv/tests/test_ltp_result.py
git commit -m "test(riscv): normalize LTP gate results"
```

### Task 2: Register Fixed LTP QEMU Profiles

**Files:**
- Modify: `tools/riscv/qemu_uboot_profiles.py`
- Modify: `tools/riscv/tests/test_qemu_uboot_contracts.py`

- [ ] **Step 1: Write failing profile-contract tests**

Add assertions that resolve `generic-sv39-ltp-smp1` and
`generic-sv39-ltp-smp4`, share only reviewed components, use the exact LTP
terminal marker, and render `-smp 1`/`-smp 4` respectively:

```python
def test_ltp_profiles_are_registered_complete_boots(self) -> None:
    for name, smp in (
        ("generic-sv39-ltp-smp1", 1),
        ("generic-sv39-ltp-smp4", 4),
    ):
        profile = profile_by_name(name)
        validate_registered_profile(profile)
        self.assertEqual(profile.hart_count, smp)
        self.assertEqual(profile.validation.completion_line, b"__LTP_GATE_DONE__")
        self.assertEqual(profile.validation.audit_policy, AuditPolicy.REGISTERED_MILESTONES)
        self.assertEqual(profile.validation.scope, ResultScope.COMPLETE_BOOT)
        argv = qemu_argv(
            uboot=Path("/u-boot"),
            boot_disk=Path("/boot.ext4"),
            profile=profile,
            snapshot_disk=False,
        )
        self.assertEqual(argv[argv.index("-smp") + 1], str(smp))
```

- [ ] **Step 2: Run the focused tests and verify RED**

Expected: `ValueError: unknown QEMU U-Boot profile`.

- [ ] **Step 3: Add the validation scenario and two immutable profiles**

Add `replace` to the dataclasses import. Define:

```python
LTP_SYSCALL_GATE = ValidationScenario(
    name="asterinas-ltp-syscall-gate",
    bootargs="console=ttyS0 loglevel=error init=/init",
    scope=ResultScope.COMPLETE_BOOT,
    milestones=(
        *_ASTERINAS_COMMON_MILESTONES,
        MilestoneExpectation(BootMilestone.KERNEL_READY, b"OSTD initialized. Preparing components."),
        MilestoneExpectation(BootMilestone.ROOTFS_READY, b"[kernel] rootfs is ready"),
        MilestoneExpectation(BootMilestone.USERSPACE_READY, b"__LTP_GATE_DONE__"),
    ),
    terminal=BootMilestone.USERSPACE_READY,
    completion_line=b"__LTP_GATE_DONE__",
    forbidden_markers=(b"Uncaught panic", b"unexpected exception"),
    audit_policy=AuditPolicy.REGISTERED_MILESTONES,
    startup_timeout=30.0,
    command_timeout=120.0,
    boot_timeout=7200.0,
    post_terminal_timeout=2.0,
)

QEMU_VIRT_SMP4 = replace(
    QEMU_VIRT,
    name="qemu-virt-smp4",
    hart_count=4,
    mmu_types=("riscv,sv39",) * 4,
)

GENERIC_SV39_LTP_SMP1 = QemuUbootProfile(
    name="generic-sv39-ltp-smp1",
    machine=QEMU_VIRT,
    boot_flow=UBOOT_BOOTI,
    validation=LTP_SYSCALL_GATE,
)
GENERIC_SV39_LTP_SMP4 = QemuUbootProfile(
    name="generic-sv39-ltp-smp4",
    machine=QEMU_VIRT_SMP4,
    boot_flow=UBOOT_BOOTI,
    validation=LTP_SYSCALL_GATE,
)
```

Register both in `_PROFILES`. Extend closed-composition tests only where the
new registered objects intentionally change the expected profile set; do not
weaken checks that reject arbitrary runtime profiles.

- [ ] **Step 4: Run contract and boot-driver tests**

```bash
PYTHONPATH=tools/riscv python3 -m unittest \
  tools.riscv.tests.test_qemu_uboot_contracts \
  tools.riscv.tests.test_qemu_uboot_booti -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit the profiles**

```bash
git add tools/riscv/qemu_uboot_profiles.py \
  tools/riscv/tests/test_qemu_uboot_contracts.py
git commit -m "test(riscv): register LTP QEMU profiles"
```

### Task 3: Permit Only the Private LTP Preparation Root

**Files:**
- Modify: `tools/riscv/prepare_qemu_uboot_booti.sh`
- Modify: `tools/riscv/tests/test_qemu_uboot_booti.py`

- [ ] **Step 1: Write failing canonical-path tests**

Use the existing subprocess helper for `--canonical-output-dir` and assert:

```python
accepted = REPO_ROOT / "target/ltp/qemu/smp1"
result = subprocess.run(
    [str(PREPARE_SCRIPT), "--canonical-output-dir", str(accepted)],
    check=True,
    capture_output=True,
    text=True,
)
self.assertEqual(Path(result.stdout.strip()), accepted)

for rejected in (
    REPO_ROOT / "target/ltp",
    REPO_ROOT / "target/ltp/not-qemu",
    REPO_ROOT / "target/qemu-uboot/../ltp/escape",
):
    with self.subTest(rejected=rejected):
        result = subprocess.run(
            [str(PREPARE_SCRIPT), "--canonical-output-dir", str(rejected)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
```

Retain the existing assertion that `target/qemu-uboot/current` remains valid
for non-LTP consumers.

- [ ] **Step 2: Run the test and verify RED**

Expected: the private LTP path is rejected.

- [ ] **Step 3: Extend the fixed allowlist without adding an environment escape hatch**

Change only `canonical_output_dir()`:

```bash
local qemu_output_root
local ltp_output_root
qemu_output_root="$(canonical_repo_path "target/qemu-uboot")"
ltp_output_root="$(canonical_repo_path "target/ltp/qemu")"
resolved="$(canonical_repo_path "${candidate}")"
case "${resolved}" in
    "${qemu_output_root}"/*|"${ltp_output_root}"/*) printf '%s\n' "${resolved}" ;;
    *)
        printf 'output directory must resolve below %s or %s\n' \
            "${qemu_output_root}" "${ltp_output_root}" >&2
        return 2
        ;;
esac
```

Do not allow `target/ltp/qemu` itself: every preparation must use an SMP/run
subdirectory, keeping the root from being cleared by the generic preparer.

- [ ] **Step 4: Run focused tests and shell syntax validation**

```bash
bash -n tools/riscv/prepare_qemu_uboot_booti.sh
PYTHONPATH=tools/riscv python3 -m unittest \
  tools.riscv.tests.test_qemu_uboot_booti -v
```

Expected: pass.

- [ ] **Step 5: Commit the path policy**

```bash
git add tools/riscv/prepare_qemu_uboot_booti.sh \
  tools/riscv/tests/test_qemu_uboot_booti.py
git commit -m "test(riscv): isolate LTP QEMU artifacts"
```

### Task 4: Select the Manifest Without Silent Omissions

**Files:**
- Create: `tools/riscv/ltp_manifest.py`
- Create: `tools/riscv/tests/test_ltp_manifest.py`

- [ ] **Step 1: Write failing selection tests with temporary fixtures**

Cover comments, duplicate enabled names, parameters, missing binaries, and an
unknown requested subset:

```python
def test_select_reports_every_unavailable_enabled_test(self) -> None:
    enabled = "# comment\nread01\nopen01\nmissing01\n"
    runtest = "read01 read01\nopen01 open01 -s\nmissing01 missing01\nnoentry01 noentry01\n"
    selection = select_manifest(enabled, runtest, available={"read01", "open01"})
    self.assertEqual(
        selection.lines,
        ("read01 read01", "open01 open01 -s"),
    )
    self.assertEqual(
        tuple((item.name, item.reason) for item in selection.unavailable),
        (("missing01", "missing-binary"),),
    )

def test_select_reports_enabled_name_absent_from_runtest(self) -> None:
    selection = select_manifest("munmap02\n", "read01 read01\n", available={"read01"})
    self.assertEqual(
        tuple((item.name, item.reason) for item in selection.unavailable),
        (("munmap02", "not-in-runtest"),),
    )

def test_select_rejects_duplicate_enabled_names(self) -> None:
    with self.assertRaisesRegex(ValueError, "duplicate enabled test"):
        select_manifest("read01\nread01\n", "read01 read01\n", available={"read01"})
```

- [ ] **Step 2: Run the test and verify RED**

Expected: missing `ltp_manifest` module.

- [ ] **Step 3: Implement selection and CLI**

Define immutable `UnavailableTest(name, reason)` and
`ManifestSelection(lines, unavailable, requested)` values and
`select_manifest(enabled_text, runtest_text, available, subset=())`.
The CLI must accept:

```text
select --enabled FILE --runtest FILE --bin-dir DIR \
       --output FILE --unavailable-output FILE \
       [--expected-count N] [--tag NAME ...]
```

It exits 2 for malformed/duplicate input, unknown subset tags, or a selected
count different from `--expected-count`. It atomically writes both the selected
manifest and a JSON unavailable list. Each enabled name absent from LTP's
runtest file is `not-in-runtest`; each mapped entry without a built binary is
`missing-binary`. Full-baseline selection may contain explicitly reported
unavailable entries, but a requested subset tag must be runnable or the command
fails.

- [ ] **Step 4: Run focused tests and verify GREEN**

Expected: pass.

- [ ] **Step 5: Commit the selector**

```bash
git add tools/riscv/ltp_manifest.py tools/riscv/tests/test_ltp_manifest.py
git commit -m "test(riscv): validate LTP manifest packaging"
```

### Task 5: Transplant and Test the Guest Runner and Builder

**Files:**
- Create: `tools/riscv/nixos/ltp/build_ltp.sh`
- Create: `tools/riscv/nixos/ltp/init_ltp.c`
- Create: `tools/riscv/nixos/ltp/ltp_runner.c`
- Create: `tools/riscv/nixos/ltp/etc-passwd`
- Create: `tools/riscv/nixos/ltp/etc-group`
- Create: `tools/riscv/tests/test_ltp_guest_runner.py`

- [ ] **Step 1: Restore the reviewed remote file contents**

```bash
git restore --source=be13b75a8 -- \
  tools/riscv/nixos/ltp/build_ltp.sh \
  tools/riscv/nixos/ltp/init_ltp.c \
  tools/riscv/nixos/ltp/ltp_runner.c \
  tools/riscv/nixos/ltp/etc-passwd \
  tools/riscv/nixos/ltp/etc-group
```

Verify the restored build script hash is
`b5cee92b72d56a5143db798d500e3a3c201544558ea00865c632201da99c0b36`.

- [ ] **Step 2: Write a host runner test before adapting the C constants**

Compile `ltp_runner.c` in a temporary directory with overridden paths and a
one-second watchdog. Create executable fixtures that emit TPASS, TFAIL, TCONF,
one process that terminates by signal, and one process that sleeps past the
watchdog. Assert the five mutually exclusive verdict lines and historical
aggregate summary.

The compile command used by the test is:

```python
subprocess.run(
    [
        CC, "-std=c11", "-Wall", "-Wextra", "-Werror",
        f'-DBIN_DIR="{bin_dir}"',
        f'-DLOG_DIR="{log_dir}"',
        "-DDEFAULT_TIMEOUT_SEC=1",
        "-o", str(runner), str(RUNNER_SOURCE),
    ],
    check=True,
)
```

Expected RED: macro redefinition warnings become errors because the source
defines the paths and timeout unconditionally.

- [ ] **Step 3: Make only the constants test-overridable**

Wrap each constant:

```c
#ifndef BIN_DIR
#define BIN_DIR "/opt/ltp/testcases/bin"
#endif
#ifndef LOG_DIR
#define LOG_DIR "/tmp/ltp_logs"
#endif
#ifndef DEFAULT_TIMEOUT_SEC
#define DEFAULT_TIMEOUT_SEC 300
#endif
```

Do not change classification or aggregate-count behavior in this baseline
task.

- [ ] **Step 4: Replace shell manifest filtering with the validated selector**

In `build_ltp.sh`, call:

```bash
python3 "${REPO_ROOT}/tools/riscv/ltp_manifest.py" select \
    --enabled "${ALL_TESTS}" \
    --runtest "${LTP_SRC}/runtest/syscalls" \
    --bin-dir "${STAGE}/opt/ltp/testcases/bin" \
    --output "${ROOTFS}/opt/ltp/runtest/syscalls" \
    --unavailable-output "${REPO_ROOT}/target/ltp/unavailable-tests.json" \
    --expected-count 767
```

Remove the old grep loop. Keep helper binary packaging from `be13b75a8`.
Use an exact-target cleanup trap for temporary staging and never reference
`target/qemu-uboot/current`.

- [ ] **Step 5: Run static and host-runner tests**

```bash
bash -n tools/riscv/nixos/ltp/build_ltp.sh
cc -std=c11 -Wall -Wextra -Werror -fsyntax-only \
  tools/riscv/nixos/ltp/init_ltp.c
PYTHONPATH=tools/riscv python3 -m unittest \
  tools.riscv.tests.test_ltp_guest_runner -v
```

Expected: pass; the host test summary must satisfy
`total = pass + plain_fail + conf + crash + timeout`.

- [ ] **Step 6: Commit the guest/build layer**

```bash
git add tools/riscv/nixos/ltp tools/riscv/tests/test_ltp_guest_runner.py
git commit -m "test(riscv): add LTP guest runner and builder"
```

### Task 6: Add the Private Gate Orchestrator

**Files:**
- Create: `tools/riscv/ltp_gate.py`
- Create: `tools/riscv/tests/test_ltp_gate.py`

- [ ] **Step 1: Write failing pure-function and dry-run tests**

Tests must cover:

```python
def test_profile_for_smp_is_closed(self) -> None:
    self.assertEqual(profile_for_smp(1), "generic-sv39-ltp-smp1")
    self.assertEqual(profile_for_smp(4), "generic-sv39-ltp-smp4")
    with self.assertRaisesRegex(ValueError, "SMP must be 1 or 4"):
        profile_for_smp(2)

def test_run_paths_never_overlap_shared_qemu_current(self) -> None:
    paths = run_paths(REPO, run_id="m1", smp=1)
    self.assertTrue(paths.prepared_dir.is_relative_to(REPO / "target/ltp/qemu"))
    self.assertTrue(paths.result_dir.is_relative_to(REPO / "target/ltp/results"))
    self.assertNotIn(REPO / "target/qemu-uboot/current", paths.all_paths())

def test_baseline_mode_ignores_ltp_failures_not_infrastructure_failures(self) -> None:
    self.assertEqual(exit_code(infrastructure_passed=True, ltp_passed=False, baseline=True), 0)
    self.assertEqual(exit_code(infrastructure_passed=False, ltp_passed=True, baseline=True), 1)
    self.assertEqual(exit_code(infrastructure_passed=True, ltp_passed=False, baseline=False), 1)
```

Mock `subprocess.run` for a `--dry-run` CLI test and assert no command contains
`target/qemu-uboot/current`.

Add a subset test that requests five known tags, produces a five-line staged
manifest and initramfs, leaves the full rootfs byte-for-byte unchanged, and
rejects an unknown/unavailable tag before preparation.

- [ ] **Step 2: Run focused tests and verify RED**

Expected: missing module.

- [ ] **Step 3: Implement the closed orchestrator**

Expose these interfaces:

```python
@dataclass(frozen=True)
class LtpRunPaths:
    prepared_dir: Path
    result_dir: Path
    initramfs: Path
    kernel: Path

    def all_paths(self) -> tuple[Path, Path, Path, Path]:
        return self.prepared_dir, self.result_dir, self.initramfs, self.kernel
```

Use these concrete implementations for the pure policy functions:

```python
_PROFILES_BY_SMP = {
    1: "generic-sv39-ltp-smp1",
    4: "generic-sv39-ltp-smp4",
}
_RUN_ID_RE = re.compile(r"[A-Za-z0-9._-]+")

def profile_for_smp(smp: int) -> str:
    try:
        return _PROFILES_BY_SMP[smp]
    except KeyError as error:
        raise ValueError("SMP must be 1 or 4") from error

def run_paths(
    repo: Path,
    *,
    run_id: str,
    smp: int,
    kernel: Path | None = None,
) -> LtpRunPaths:
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("run id must contain only letters, digits, dot, underscore, or hyphen")
    profile_for_smp(smp)
    target = repo.resolve() / "target" / "ltp"
    return LtpRunPaths(
        prepared_dir=target / "qemu" / f"smp{smp}",
        result_dir=target / "results" / run_id,
        initramfs=target / "ltp-initramfs.cpio.gz",
        kernel=(
            repo.resolve() / "target/osdk/aster-kernel-osdk-bin.Image"
            if kernel is None
            else kernel.resolve()
        ),
    )

def exit_code(*, infrastructure_passed: bool, ltp_passed: bool, baseline: bool) -> int:
    if not infrastructure_passed:
        return 1
    return 0 if baseline or ltp_passed else 1
```

The CLI entrypoint parses only the options listed below, delegates path and
exit policy to these functions, and executes the five fixed stages in order.

Supported CLI:

```text
ltp_gate.py run --kernel IMAGE [--smp {1,4}] [--run-id ID]
                [--skip-build] [--baseline] [--boot-timeout SECONDS]
                [--tag NAME ...] [--source-commit FULL_OBJECT_ID]
ltp_gate.py build [--skip-compile]
```

For `run`, create the result directory with `mkdir(exist_ok=False)`, then run
these exact stages:

1. `build_ltp.sh` unless `--skip-build`.
   When one or more `--tag` options are present, copy the completed full rootfs
   into a `TemporaryDirectory` below `target/ltp/build`, use
   `ltp_manifest.py select --tag ...` to replace only the staged manifest, and
   pack a run-specific initramfs. `shutil.copytree(..., symlinks=True)` preserves
   the musl loader and BusyBox links; the full rootfs is opened read-only and
   its tree hash is checked before and after subset packaging.
2. `prepare_qemu_uboot_booti.sh prepare` with
   `QEMU_UBOOT_PROFILE`, `QEMU_UBOOT_OUT_DIR`,
   `ASTERINAS_RISCV_BOOTI`, and `ASTERINAS_INITRAMFS` set explicitly.
3. `qemu_uboot_booti.py run` using the private prepared inputs and writing
   `serial.log`, `marker-event.txt`, and `boot-result.json` under the new result
   directory.
4. `ltp_result.py write` to publish `result.json` and `summary.txt`.
5. Write `SHA256SUMS` using repository-relative names only.

Reject a symlinked result directory, a run id outside `[A-Za-z0-9._-]+`, a
kernel outside the repository, non-SMP 1/4, and any resolved path outside
`target/ltp`. Resolve and validate the source commit before creating the result
directory or starting QEMU. `ASTERINAS_SOURCE_COMMIT` (or the equivalent
`--source-commit`) supports isolated worktrees when their administrative Git
directory is outside a container bind mount. Always allow the guarded QEMU
runner to reap its process group before parsing evidence.

- [ ] **Step 4: Run gate unit tests and verify GREEN**

Expected: pass without invoking QEMU.

- [ ] **Step 5: Commit the orchestrator**

```bash
git add tools/riscv/ltp_gate.py tools/riscv/tests/test_ltp_gate.py
git commit -m "test(riscv): add isolated LTP gate orchestration"
```

### Task 7: Expand the Manifest to the Reviewed M27 Set

**Files:**
- Modify: `test/initramfs/src/conformance/ltp/testcases/all.txt`
- Modify: `tools/riscv/tests/test_ltp_manifest.py`

- [ ] **Step 1: Add a failing repository-manifest contract test**

Assert exactly 779 uncommented names, uniqueness, and representative M27 tags
from the network, loop, procfs, exec, scheduling, and memory buckets. The
runtime count is allowed to be 767 only after matching the LTP runtest file and
built binaries.

- [ ] **Step 2: Run the test and verify RED**

Expected: current count is 544.

- [ ] **Step 3: Restore only the reviewed manifest**

```bash
git restore --source=a4ef18017 -- \
  test/initramfs/src/conformance/ltp/testcases/all.txt
```

Review the diff to ensure no report or unrelated remote file was restored.

- [ ] **Step 4: Run the manifest tests and verify GREEN**

Expected: 779 active unique names and all representative tags present.

- [ ] **Step 5: Commit the manifest expansion**

```bash
git add test/initramfs/src/conformance/ltp/testcases/all.txt \
  tools/riscv/tests/test_ltp_manifest.py
git commit -m "test(ltp): enable reviewed RISC-V syscall set"
```

### Task 8: Add Make Targets and Operator Documentation

**Files:**
- Modify: `Makefile`
- Create: `tools/riscv/ltp/README.md`
- Modify: `tools/riscv/README.md`

- [ ] **Step 1: Add the unit target**

Add:

```make
.PHONY: test_riscv_ltp_unit
test_riscv_ltp_unit:
	@PYTHONPATH=tools/riscv python3 -m unittest \
		tools.riscv.tests.test_ltp_result \
		tools.riscv.tests.test_ltp_manifest \
		tools.riscv.tests.test_ltp_gate \
		tools.riscv.tests.test_ltp_guest_runner -v
```

Add a runtime target requiring `ASTERINAS_RISCV_BOOTI`:

```make
.PHONY: test_riscv_ltp
test_riscv_ltp: test_riscv_ltp_unit
	@test -n "$(ASTERINAS_RISCV_BOOTI)" || \
		{ echo "ASTERINAS_RISCV_BOOTI is required" >&2; exit 2; }
	@python3 tools/riscv/ltp_gate.py run \
		--kernel "$(ASTERINAS_RISCV_BOOTI)" --smp "$(SMP)"
```

- [ ] **Step 2: Document exact container, build, baseline, strict, and result commands**

The README must distinguish:

- `--baseline`: infrastructure must pass; LTP failures are recorded.
- strict/default: any LTP failure returns nonzero.
- `fail` versus `legacy_fail_total`.
- full SMP=1 versus SMP=4 smoke/full runs.
- ignored evidence locations and tracked report provenance.

Include only repository-relative paths and the pinned LTP tag 20260529.

- [ ] **Step 3: Verify documentation commands and Make parsing**

```bash
make -n test_riscv_ltp_unit
make -n test_riscv_ltp ASTERINAS_RISCV_BOOTI=target/osdk/aster-kernel-osdk-bin.Image SMP=1
python3 tools/riscv/ltp_gate.py --help
git diff --check
```

Expected: commands parse and diff check is clean.

- [ ] **Step 4: Commit operator integration**

```bash
git add Makefile tools/riscv/README.md tools/riscv/ltp/README.md
git commit -m "docs(riscv): document the LTP gate"
```

### Task 9: Run the Complete Host Verification Gate

**Files:** none unless a test exposes a defect.

- [ ] **Step 1: Run the focused LTP unit target**

```bash
make test_riscv_ltp_unit
```

Expected: all new tests pass.

- [ ] **Step 2: Run the complete RISC-V tooling suite**

```bash
PYTHONPATH=tools/riscv python3 -m unittest discover \
  -s tools/riscv/tests -p 'test_*.py' -v
```

Expected: the pre-existing suite and all LTP tests pass; skipped tests retain
their documented prerequisite reasons.

- [ ] **Step 3: Run static checks**

```bash
bash -n tools/riscv/prepare_qemu_uboot_booti.sh
bash -n tools/riscv/nixos/ltp/build_ltp.sh
cc -std=c11 -Wall -Wextra -Werror -fsyntax-only \
  tools/riscv/nixos/ltp/init_ltp.c
ltp_integration_base=$(git merge-base HEAD codex/megrez-usb-keyboard)
git diff --check "${ltp_integration_base}..HEAD"
```

Expected: clean.

- [ ] **Step 4: Fix any defect test-first and commit only the affected files**

Do not proceed to QEMU while the host gate is red.

### Task 10: Build the Current Kernel and 767-Test Initramfs

**Files:** ignored artifacts under `target/ltp` and `target/osdk` only.

- [ ] **Step 1: Obtain the pinned LTP source if absent**

```bash
git clone --depth 1 --branch 20260529 \
  https://github.com/linux-test-project/ltp.git target/ltp/src
git -C target/ltp/src describe --tags --exact-match
```

Expected: `20260529`.

- [ ] **Step 2: Prepare the pinned RISC-V musl wrapper and sysroot**

```bash
mkdir -p target/ltp/toolchain/package target/ltp/toolchain/root
curl --fail --location \
  --output target/ltp/toolchain/package/musl-riscv64-1.2.6-1-x86_64.pkg.tar.zst \
  https://archlinux.org/packages/extra/x86_64/musl-riscv64/download/
printf '%s  %s\n' \
  0797f54b48c415739bb5360739bc8f9dc8b2019e01de86d89c2859810200b589 \
  target/ltp/toolchain/package/musl-riscv64-1.2.6-1-x86_64.pkg.tar.zst \
  | sha256sum -c -
tar --extract \
  --file target/ltp/toolchain/package/musl-riscv64-1.2.6-1-x86_64.pkg.tar.zst \
  --directory target/ltp/toolchain/root
```

Expected: the checksum passes and the extracted tree contains
`usr/bin/riscv64-linux-musl-gcc` plus `usr/riscv64-linux-musl/`.

- [ ] **Step 3: Build the current Sv39 RISC-V kernel in the project container**

```bash
docker run --rm --privileged --network=host -v /dev:/dev \
  -v "$(pwd):/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc \
  bash -lc 'restore_owner() { chown -R --reference=/root/asterinas \
      /root/asterinas/target/osdk 2>/dev/null || true; }; \
    trap restore_owner EXIT; \
    test -s "${VDSO_LIBRARY_DIR}/vdso_riscv64.so"; \
    make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode'
```

Expected: `target/osdk/aster-kernel-osdk-bin.Image` exists and passes the
repository Linux Image header validator.

- [ ] **Step 4: Build and package the expanded LTP set**

Use the same local cross-build image with the pinned musl materials mounted
read-only:

```bash
docker run --rm --network=host \
  -v "$(pwd):/root/asterinas" -w /root/asterinas \
  -v "$(pwd)/target/ltp/toolchain/root/usr/bin/riscv64-linux-musl-gcc:\
/usr/bin/riscv64-linux-musl-gcc:ro" \
  -v "$(pwd)/target/ltp/toolchain/root/usr/riscv64-linux-musl:\
/usr/riscv64-linux-musl:ro" \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc \
  bash -lc 'apt-get update -qq; \
    apt-get install -y --no-install-recommends \
      autoconf automake linux-libc-dev-riscv64-cross; \
    restore_owner() { chown -R --reference=/root/asterinas \
      /root/asterinas/target/ltp 2>/dev/null || true; }; \
    trap restore_owner EXIT; \
    tools/riscv/nixos/ltp/build_ltp.sh'
```

Expected:

- `target/ltp/ltp-initramfs.cpio.gz` is nonempty;
- the selected runtime manifest contains 767 entries;
- `target/ltp/unavailable-tests.json` records every one of the 12 enabled but
  unpackaged names: `munmap02` as `not-in-runtest` and 11 build-missing entries
  as `missing-binary`;
- execve/execveat helper binaries are present;
- initramfs validation succeeds.

The fixed-source comparison must start from 779 unique enabled names, find 778
in LTP's `runtest/syscalls`, and package 767 built binaries. If any count or
unavailable reason differs, stop and classify it. Do not change the expected
count or silently omit a binary.

### Task 11: Capture the SMP=1 Baseline and SMP=4 Smoke

**Files:**
- Create after real evidence: `tools/riscv/ltp/BASELINE-M1-report.md`

- [ ] **Step 1: Run the full SMP=1 baseline**

```bash
python3 tools/riscv/ltp_gate.py run \
  --kernel target/osdk/aster-kernel-osdk-bin.Image \
  --smp 1 --run-id baseline-m1-smp1 --skip-build --baseline
```

Expected infrastructure evidence:

- exactly one U-Boot `booti`;
- exactly one `__LTP_GATE_DONE__` and one PASS/FAIL marker;
- `total=767` with consistent normalized counts;
- no kernel panic or unexpected exception;
- QEMU process-group cleanup complete;
- immutable kernel/DTB/initramfs and prepared-disk identities;
- `serial.log`, `summary.txt`, `result.json`, `boot-result.json`,
  `marker-event.txt`, and `SHA256SUMS` under
  `target/ltp/results/baseline-m1-smp1/`.

- [ ] **Step 2: Run the SMP=4 smoke subset**

Run the implemented subset path:

```bash
python3 tools/riscv/ltp_gate.py run \
  --kernel target/osdk/aster-kernel-osdk-bin.Image \
  --smp 4 --run-id baseline-m1-smp4-smoke --skip-build --baseline \
  --boot-timeout 600 \
  --tag getpid01 --tag read01 --tag write01 \
  --tag uname01 --tag clock_gettime01
```

Expected: five verdicts, a completion marker, an unchanged full-rootfs hash,
and clean QEMU teardown.

If SMP=4 smoke hangs or panics, retain its evidence and classify it; do not run
the full SMP=4 suite. If it completes, run the full 767-test SMP=4 baseline
with `--baseline` and a 7200-second timeout.

- [ ] **Step 3: Write the tracked baseline report from actual results**

The report must contain:

- source branch and commit;
- LTP tag and active/packaged counts;
- exact normalized SMP=1 counts and historical aggregate count;
- SMP=4 smoke/full disposition;
- artifact hashes and result-directory names;
- infrastructure failures, kernel panics, and hangs separately from LTP FAIL,
  CONF, CRASH, and per-test TIMEOUT;
- the next point-fix work queue derived only from observed failures.

Do not copy the remote M27 counts as the current baseline.

- [ ] **Step 4: Verify report claims against result JSON and hashes**

```bash
python3 -m json.tool target/ltp/results/baseline-m1-smp1/result.json >/dev/null
sha256sum -c target/ltp/results/baseline-m1-smp1/SHA256SUMS
rg -n 'Uncaught panic|unexpected exception' \
  target/ltp/results/baseline-m1-smp1/serial.log
git diff --check -- tools/riscv/ltp/BASELINE-M1-report.md
```

Expected: JSON and hashes validate; panic search has no matches; report diff is
clean.

- [ ] **Step 5: Commit only the evidence-backed report**

```bash
git add tools/riscv/ltp/BASELINE-M1-report.md
git commit -m "docs(riscv): record initial 767-test LTP baseline"
```

### Task 12: Review the Gate Before Point-Fix Planning

**Files:** review output only if the review skill requires a tracked file.

- [ ] **Step 1: Run Asterinas persona-based review on the complete plan range**

Review maintainability, kernel correctness, security, hardware contract, and
documentation. Pay special attention to output-directory containment,
symlink races, process cleanup, result count normalization, and the difference
between infrastructure success and LTP success.

- [ ] **Step 2: Resolve every Critical or Important finding test-first**

Re-run the smallest focused test after each fix, then the full host suite.

- [ ] **Step 3: Run final verification from the isolated worktree**

```bash
make test_riscv_ltp_unit
PYTHONPATH=tools/riscv python3 -m unittest discover \
  -s tools/riscv/tests -p 'test_*.py' -v
ltp_integration_base=$(git merge-base HEAD codex/megrez-usb-keyboard)
git diff --check "${ltp_integration_base}..HEAD"
git status --short --branch
```

Expected: tests green, diff clean, and only explicitly ignored runtime
artifacts remain untracked.

- [ ] **Step 4: Produce the next plan from the real baseline**

Write a separate point-fix plan grouping only observed failures by independent
root cause. Keep loop-device integration in its own later plan.
