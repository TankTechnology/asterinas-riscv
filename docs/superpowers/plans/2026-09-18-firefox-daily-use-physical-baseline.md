# Firefox Daily-Use Physical Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fail-closed guest-to-host evidence path and use it to collect three qualified Firefox daily-use profiles on the physical four-hart Megrez board, producing an auditable baseline and an evidence-based bottleneck classification.

**Architecture:** A closed guest action runs the existing daily-use gate and uploads one canonical, bounded evidence bundle to a fixture endpoint bound to the board peer and a host-generated experiment ID. A dedicated host runner composes the existing physical graphics boot and recovery operations, validates and privately publishes the bundle, while a separate analyzer accepts only three qualified runs and reports raw metrics plus `executing`, `runnable-delayed`, `sleeping-blocking`, or `mixed` evidence. This is Phase 1 of the approved design; the observed classification determines a separate Phase 2 plan for one kernel change and controlled A/B validation.

**Tech Stack:** Python 3 standard library (`http.server`, `dataclasses`, `hashlib`, `json`, `unittest`), POSIX shell, GNU Make, existing Megrez serial/boot tooling, Docker-based RISC-V build, QEMU, and the physical Megrez board.

---

## Scope and file map

Implementation happens only in the isolated `codex/firefox-daily-use-perf` worktree. Do not edit or clean the user's dirty `main` worktree.

| File | Responsibility |
|---|---|
| `tools/riscv/debian/rootfs/browser_daily_use_upload.py` | Closed bundle construction, validation, decoding, and one-shot HTTP upload; shared by guest and host tests. |
| `tools/riscv/tests/test_browser_daily_use_upload.py` | Exact-schema, artifact-order, regular-file, mode, size, digest, base64, result-contract, and upload tests. |
| `tools/riscv/megrez_network_fixture.py` | Expected-experiment one-shot evidence endpoint, peer binding, and retained upload summary. |
| `tools/riscv/tests/test_megrez_network_fixture.py` | Real loopback HTTP rejection/acceptance tests for the new endpoint. |
| `tools/riscv/debian/rootfs/physical_graphics_control.sh` | Closed `daily-use` guest action; PID/Xorg checks; gate and uploader invocation. |
| `tools/riscv/debian/rootfs/physical_external_services_quiesce.sh` | Preserve public-network blocking while allowing the frozen host fixture in daily-use boots. |
| `tools/riscv/debian/rootfs/build_stage1.sh` | Install the uploader as a private Stage1-bound executable. |
| `tools/riscv/tests/test_debian_rootfs.py` | Stage1 archive and fixed-action grammar tests. |
| `tools/riscv/megrez_physical_graphics.py` | One public operation for sending the fixed daily-use action and parsing its terminal status; existing boot/recovery behavior remains authoritative. |
| `tools/riscv/megrez_firefox_daily_use.py` | Dedicated host lifecycle, fixture coordination, bundle validation, run qualification, and private publication. |
| `tools/riscv/tests/test_megrez_firefox_daily_use.py` | Mocked lifecycle, timeout, recovery, identity, failure, and publication tests. |
| `tools/riscv/megrez_firefox_daily_use_report.py` | Three-run qualification, metric extraction, range/median calculation, and conservative mechanism classification. |
| `tools/riscv/tests/test_megrez_firefox_daily_use_report.py` | Qualification and classifier boundary tests with synthetic evidence. |
| `Makefile` | Unit target and prepare-only command for the physical baseline runner. |
| `tools/riscv/firefox_fast_check.sh` | Add the new unit modules to the short regression gate. |
| `tools/riscv/README.md` | Exact build, prepare, physical-run, qualification, and recovery commands. |
| `docs/performance/2026-09-18-firefox-daily-use-physical-baseline.md` | Immutable identities, three raw-run links, summaries, limitations, and optimization-admission decision. |

The upload module owns byte-level evidence validation. The fixture owns HTTP framing and one-shot receipt. The physical runner owns lifecycle and publication. The report module owns cross-run analysis; it never changes or repairs run evidence.

### Task 1: Closed daily-use evidence bundle

**Files:**
- Create: `tools/riscv/debian/rootfs/browser_daily_use_upload.py`
- Create: `tools/riscv/tests/test_browser_daily_use_upload.py`

- [ ] **Step 1: Write failing tests for the success and failure bundle contracts**

Create tests using `tempfile.TemporaryDirectory`, the existing `complete_result()` helper pattern from `tools/riscv/tests/test_browser_daily_use_gate.py`, and private `0o600` files. The tests must contain these assertions:

```python
SUCCESS_NAMES = (
    "browser-fixture-capture.json",
    "browser-local-capture.json",
    "browser-context-switch.json",
    "browser-composite-capture.json",
    "browser-system-time.json",
    "browser-thread-time.json",
    "browser-daily-use-result.json",
)

def test_build_and_parse_success_bundle_preserves_closed_order(self):
    evidence = self.make_success_evidence()
    raw = self.module.build_bundle(evidence, EXPERIMENT_ID, "pass")
    parsed = self.module.parse_bundle(raw, EXPERIMENT_ID)
    self.assertEqual(parsed.experiment_id, EXPERIMENT_ID)
    self.assertEqual(parsed.gate_run_id, GATE_RUN_ID)
    self.assertEqual(parsed.outcome, "pass")
    self.assertEqual(tuple(parsed.artifacts), SUCCESS_NAMES)
    self.assertEqual(parsed.artifacts[SUCCESS_NAMES[-1]], evidence.joinpath(SUCCESS_NAMES[-1]).read_bytes())

def test_failure_bundle_contains_only_checkpoint(self):
    evidence = self.make_failure_evidence()
    raw = self.module.build_bundle(evidence, EXPERIMENT_ID, "fail")
    parsed = self.module.parse_bundle(raw, EXPERIMENT_ID)
    self.assertEqual(parsed.outcome, "fail")
    self.assertEqual(tuple(parsed.artifacts), ("browser-daily-use-checkpoint.json",))

def test_result_manifest_mismatch_is_rejected(self):
    evidence = self.make_success_evidence()
    result = json.loads(evidence.joinpath(SUCCESS_NAMES[-1]).read_text())
    result["artifacts"][0]["sha256"] = "0" * 64
    self.write_private(evidence / SUCCESS_NAMES[-1], canonical_json(result), replace=True)
    with self.assertRaisesRegex(self.module.EvidenceBundleError, "manifest"):
        self.module.build_bundle(evidence, EXPERIMENT_ID, "pass")
```

Add separate tests rejecting an uppercase/short experiment ID, unknown field, duplicate artifact name, reordered success artifacts, invalid base64, size mismatch, digest mismatch, symlink, non-regular file, group/world permission bits, empty file, artifact larger than 64 MiB, canonical bundle larger than 2 MiB, result `runId` mismatch, and a failure bundle containing a success artifact.

- [ ] **Step 2: Run the focused tests and verify the import fails**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_browser_daily_use_upload -v
```

Expected: `ERROR` with `ModuleNotFoundError` for `browser_daily_use_upload`.

- [ ] **Step 3: Implement the exact bundle API and fail-closed file reader**

Create the module with these public names and types:

```python
SCHEMA_VERSION = 1
MAX_BUNDLE_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
SUCCESS_ARTIFACT_NAMES = (*ARTIFACT_NAMES, RESULT_NAME)
FAILURE_ARTIFACT_NAMES = (CHECKPOINT_NAME,)

class EvidenceBundleError(ValueError):
    pass

@dataclass(frozen=True)
class EvidenceBundle:
    experiment_id: str
    gate_run_id: str
    outcome: str
    artifacts: dict[str, bytes]

def build_bundle(evidence_dir: Path, experiment_id: str, outcome: str) -> bytes:
    """Build canonical JSON from the closed success or failure file set."""

def parse_bundle(raw: bytes, expected_experiment_id: str) -> EvidenceBundle:
    """Validate exact schema, canonical encoding, content, hashes, and result contract."""

def upload_bundle(raw: bytes, url: str, timeout_seconds: float) -> None:
    """POST one fixed-length application/json body and require HTTP 204."""

def main(argv: list[str] | None = None) -> int:
    """CLI: EVIDENCE_DIR EXPERIMENT_ID OUTCOME URL --timeout SECONDS."""
```

Use `os.open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK)`, then `os.fstat`. Require a regular file, size in `1..MAX_FILE_BYTES`, and `(st_mode & 0o077) == 0`. Read through the held descriptor, compare `st_dev`, `st_ino`, `st_size`, `st_mtime_ns`, and `st_ctime_ns` before/after, then verify the pathname still resolves to the same device/inode without following symlinks.

Serialize with exactly:

```python
def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
```

Each artifact object has exactly `name`, `bytes`, `sha256`, and `base64`. For a pass, call `validate_daily_use_result`, require `state == "pass"`, require `runId == gateRunId`, and compare the six manifest rows against the six component payloads. For a failure, parse the checkpoint as an exact JSON object, require its 32-lowercase-hex `runId`, and send only the checkpoint. `parse_bundle` must re-encode the decoded object and reject non-canonical input before returning detached bytes.

The HTTP client must accept only an `http://` URL with no userinfo, fragment, or query; set `Content-Type: application/json` and one decimal `Content-Length`; use `http.client.HTTPConnection`; require status `204` and an empty response body.

- [ ] **Step 4: Run upload tests and the existing daily-use contract tests**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_browser_daily_use_upload \
  tools.riscv.tests.test_browser_daily_use_contract \
  tools.riscv.tests.test_browser_daily_use_gate -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit the bundle contract**

```bash
git add tools/riscv/debian/rootfs/browser_daily_use_upload.py \
  tools/riscv/tests/test_browser_daily_use_upload.py
git commit -m "Add closed daily-use evidence bundle"
```

### Task 2: One-shot fixture upload endpoint

**Files:**
- Modify: `tools/riscv/megrez_network_fixture.py`
- Modify: `tools/riscv/tests/test_megrez_network_fixture.py`

- [ ] **Step 1: Write failing real-HTTP endpoint tests**

Use the existing loopback fixture helper and a fixed `EXPERIMENT_ID`. Add tests with these observable behaviors:

```python
def test_daily_use_evidence_accepts_one_matching_upload(self):
    body = b'{"schemaVersion":1}\n'
    response = self.post(
        f"/browser-quality/daily-use-evidence/{EXPERIMENT_ID}", body
    )
    self.assertEqual(response.status, 204)
    self.assertEqual(self.server.daily_use_evidence_payload(), body)
    self.assertEqual(
        self.server.daily_use_evidence_summary(),
        {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()},
    )

def test_daily_use_evidence_rejects_second_upload(self):
    path = f"/browser-quality/daily-use-evidence/{EXPERIMENT_ID}"
    self.assertEqual(self.post(path, b"{}\n").status, 204)
    self.assertEqual(self.post(path, b"{}\n").status, 409)
```

Add individual tests for wrong experiment ID (`404`), any query (`400`), wrong peer (`403`), missing/repeated/invalid `Content-Length` (`411`/`400`), `Transfer-Encoding` (`400`), zero body (`400`), body over 2 MiB (`413`), and endpoint disabled when no experiment ID is configured (`404`). Confirm the existing screenshot endpoints still pass.

- [ ] **Step 2: Run the two new tests and verify failure**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_network_fixture.MegrezNetworkFixtureTests.test_daily_use_evidence_accepts_one_matching_upload \
  tools.riscv.tests.test_megrez_network_fixture.MegrezNetworkFixtureTests.test_daily_use_evidence_rejects_second_upload -v
```

Expected: failures because `FixtureConfig` and `FixtureServer` do not expose daily-use evidence state.

- [ ] **Step 3: Add endpoint configuration and atomic one-shot retention**

Add these constants and configuration/state fields:

```python
BROWSER_DAILY_USE_EVIDENCE_PREFIX = "/browser-quality/daily-use-evidence/"
MAX_DAILY_USE_EVIDENCE_BYTES = 2 * 1024 * 1024
EXPERIMENT_ID_RE = re.compile(r"[0-9a-f]{32}\Z")

@dataclass(frozen=True)
class FixtureConfig:
    bind_address: str = "127.0.0.1"
    port: int = 17894
    allowed_peer: str | None = None
    daily_use_experiment_id: str | None = None
```

Validate the configured ID at server construction. Store the body while holding the existing state lock; reject a second body before replacing state. Expose:

```python
def daily_use_evidence_payload(self) -> bytes | None:
    with self._lock:
        return self._daily_use_evidence

def daily_use_evidence_summary(self) -> dict[str, object] | None:
    payload = self.daily_use_evidence_payload()
    if payload is None:
        return None
    return {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}

def wait_for_daily_use_evidence(self, timeout_seconds: float) -> bytes:
    deadline = time.monotonic() + timeout_seconds
    with self._daily_use_condition:
        while self._daily_use_evidence is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("daily-use evidence upload timed out")
            self._daily_use_condition.wait(remaining)
        return self._daily_use_evidence
```

Create `_daily_use_condition = threading.Condition(self._lock)`. Route only an exact path match assembled from the configured ID. Parse the raw request target before `urlsplit`, so any `?` is rejected. Require exactly one `Content-Length` header via `self.headers.get_all("Content-Length", [])`; reject any `Transfer-Encoding`; read exactly the declared bytes and reject a premature EOF. Notify the condition and return `204` with `Content-Length: 0` only after atomic retention.

Add CLI flag `--daily-use-experiment-id` and pass it into `FixtureConfig`.

- [ ] **Step 4: Run all fixture tests**

```bash
python3 -m unittest tools.riscv.tests.test_megrez_network_fixture -v
```

Expected: all tests pass, including the existing network and screenshot endpoints.

- [ ] **Step 5: Commit the one-shot endpoint**

```bash
git add tools/riscv/megrez_network_fixture.py \
  tools/riscv/tests/test_megrez_network_fixture.py
git commit -m "Add bound daily-use evidence endpoint"
```

### Task 3: Fixed guest daily-use action and Stage1 packaging

**Files:**
- Modify: `tools/riscv/debian/rootfs/physical_graphics_control.sh`
- Modify: `tools/riscv/debian/rootfs/physical_external_services_quiesce.sh`
- Modify: `tools/riscv/debian/rootfs/build_stage1.sh`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`

- [ ] **Step 1: Write failing shell-contract and archive tests**

Add tests that execute a copied control script with fake `systemctl`, `pgrep`, `nsenter`, gate, and uploader commands. Assert the only accepted grammar is:

```text
daily-use 0123456789abcdef0123456789abcdef 120 4242
```

Cover short/uppercase/non-hex IDs, `0`, `120.1`, and `121` timeouts, missing/extra arguments, mismatched Firefox PID, zero/multiple Xorg PIDs, pre-existing evidence directory, a missing systemd fixture environment, and fixture source URLs that are not exactly `http://10.100.19.216:17894/asterinas-network-probe.bin`.

For the success path assert the fake log contains these exact logical invocations in order:

```text
nsenter -t 4242 -n /run/asterinas-tools/browser-daily-use-gate --firefox-pid 4242 --xorg-pid 4343 --fixture-index-url http://10.100.19.216:17894/browser-quality/index.html --evidence-dir /run/asterinas-browser-daily-use-0123456789abcdef0123456789abcdef --mode profile --physical --timeout-seconds 120
nsenter -t 4242 -n /run/asterinas-tools/browser-daily-use-upload /run/asterinas-browser-daily-use-0123456789abcdef0123456789abcdef 0123456789abcdef0123456789abcdef pass http://10.100.19.216:17894/browser-quality/daily-use-evidence/0123456789abcdef0123456789abcdef --timeout 15
```

Assert a daily-use boot makes `physical_external_services_quiesce.sh` write a browser drop-in with proxy mode, proxy host `10.100.19.216`, and proxy port `9`. This blocks public destinations at an unused port while Firefox's frozen `network.proxy.no_proxies_on` rule bypasses the proxy only for the fixture host. Assert an ordinary physical-graphics boot retains proxy host `127.0.0.1` and port `9`.

Assert the serial line is exactly:

```text
__ASTERINAS_PHYSICAL_DAILY_USE__ experiment_id=0123456789abcdef0123456789abcdef outcome=pass gate_status=0 upload_status=0
```

For a gate failure, assert the uploader receives `fail`, the terminal outcome remains `fail`, and the action exits nonzero even when upload succeeds. Add an archive assertion for `/run/asterinas-tools/browser-daily-use-upload`, regular mode `0o755`, deterministic timestamp, and no duplicate member.

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_rootfs.DebianStage1Tests.test_stage1_includes_daily_use_uploader \
  tools.riscv.tests.test_debian_rootfs.DebianStage1Tests.test_physical_graphics_control_accepts_closed_daily_use_action \
  tools.riscv.tests.test_debian_rootfs.DebianStage1Tests.test_physical_graphics_control_rejects_untrusted_daily_use_arguments \
  tools.riscv.tests.test_debian_rootfs.DebianStage1Tests.test_physical_graphics_control_preserves_failed_daily_use_outcome \
  tools.riscv.tests.test_debian_rootfs.DebianStage1Tests.test_daily_use_quiesce_allows_only_fixture_host -v
```

Expected: failures because the action and Stage1 member do not exist.

- [ ] **Step 3: Implement strict validators and the daily-use action**

Add:

```sh
is_experiment_id() {
    [ "${#1}" -eq 32 ] || return 1
    case "$1" in
        *[!0-9a-f]*) return 1 ;;
        *) return 0 ;;
    esac
}

is_profile_timeout() {
    is_uint "$1" && [ "$1" -ge 1 ] && [ "$1" -le 120 ]
}
```

Implement the action with this closed data flow; use distinct nonzero prerequisite status codes but preserve the printed field names:

```sh
daily_use() {
    [ "$#" -eq 3 ] || die_usage
    experiment_id=$1
    timeout_seconds=$2
    expected_pid=$3
    is_experiment_id "$experiment_id" || die_usage
    is_profile_timeout "$timeout_seconds" || die_usage
    is_uint "$expected_pid" || die_usage
    [ "$expected_pid" -gt 1 ] || die_usage

    fixture_source='http://10.100.19.216:17894/asterinas-network-probe.bin'
    fixture_index='http://10.100.19.216:17894/browser-quality/index.html'
    upload_url="http://10.100.19.216:17894/browser-quality/daily-use-evidence/$experiment_id"
    evidence_dir="/run/asterinas-browser-daily-use-$experiment_id"
    gate_status=125
    upload_status=125
    outcome=fail

    browser_identity
    original_pid=$pid
    case "$original_pid" in
        '' | *[!0-9]*) gate_status=124 ;;
        *)
            if [ "$original_pid" != "$expected_pid" ] || [ "$original_pid" -le 1 ]; then
                gate_status=124
            elif [ -e "$evidence_dir" ] || [ -L "$evidence_dir" ]; then
                gate_status=123
            else
                manager_environment=$(systemctl_bounded show-environment 2>/dev/null || true)
                physical_mode=$(printf '%s\n' "$manager_environment" |
                    sed -n 's/^ASTERINAS_PHYSICAL_DAILY_USE=//p')
                configured_fixture=$(printf '%s\n' "$manager_environment" |
                    sed -n 's/^ASTERINAS_DESKTOP_FIXTURE_URL=//p')
                xorg_pids=$(pgrep -x Xorg 2>/dev/null || true)
                set -- $xorg_pids
                if [ "$#" -ne 1 ] || ! is_uint "$1"; then
                    gate_status=122
                elif [ "$physical_mode" != 1 ] || [ "$configured_fixture" != "$fixture_source" ]; then
                    gate_status=121
                else
                    xorg_pid=$1
                    PYTHONPYCACHEPREFIX=/run/asterinas-python-cache \
                        nsenter -t "$original_pid" -n \
                        /run/asterinas-tools/browser-daily-use-gate \
                        --firefox-pid "$original_pid" --xorg-pid "$xorg_pid" \
                        --fixture-index-url "$fixture_index" \
                        --evidence-dir "$evidence_dir" --mode profile --physical \
                        --timeout-seconds "$timeout_seconds"
                    gate_status=$?
                    [ "$gate_status" -ne 0 ] || outcome=pass
                    PYTHONPYCACHEPREFIX=/run/asterinas-python-cache \
                        nsenter -t "$original_pid" -n \
                        /run/asterinas-tools/browser-daily-use-upload \
                        "$evidence_dir" "$experiment_id" "$outcome" "$upload_url" \
                        --timeout 15
                    upload_status=$?
                fi
            fi
            ;;
    esac

    printf '__ASTERINAS_PHYSICAL_DAILY_USE__ experiment_id=%s outcome=%s gate_status=%s upload_status=%s\n' \
        "$experiment_id" "$outcome" "$gate_status" "$upload_status"
    [ "$outcome" = pass ] && [ "$gate_status" -eq 0 ] && [ "$upload_status" -eq 0 ]
}
```

This reads the current Firefox service PID and compares it to the expected PID, requires one Xorg PID, reads the systemd manager's local fixture configuration, and never accepts a host-provided URL, path, mode, or Xorg PID.

Run the gate through `nsenter -t "$original_pid" -n` and capture its status without allowing `set -e` behavior. Select `pass` only for gate status zero. Always attempt the bounded uploader after the gate. Print exactly one terminal status containing experiment ID, outcome, gate status, and upload status. Return success only when both statuses are zero and outcome is `pass`.

In `physical_external_services_quiesce.sh`, select proxy host `10.100.19.216` only when the systemd manager environment contains both exact daily-use values above; otherwise retain `127.0.0.1`. Keep port `9` in both cases:

```sh
_asterinas_proxy_host=127.0.0.1
_asterinas_manager_environment="$(systemctl_bounded show-environment 2>/dev/null || true)"
_asterinas_physical_mode="$(printf '%s\n' "$_asterinas_manager_environment" |
    sed -n 's/^ASTERINAS_PHYSICAL_DAILY_USE=//p')"
_asterinas_fixture_url="$(printf '%s\n' "$_asterinas_manager_environment" |
    sed -n 's/^ASTERINAS_DESKTOP_FIXTURE_URL=//p')"
if [[ "$_asterinas_physical_mode" == 1 &&
      "$_asterinas_fixture_url" == 'http://10.100.19.216:17894/asterinas-network-probe.bin' ]]; then
    _asterinas_proxy_host=10.100.19.216
fi
printf '%s\n' \
    '[Service]' \
    'Environment=HOME=/run/asterinas-physical-home' \
    'Environment=ASTERINAS_WEB_NETWORK_MODE=proxy' \
    "Environment=ASTERINAS_DESKTOP_PROXY_HOST=$_asterinas_proxy_host" \
    'Environment=ASTERINAS_DESKTOP_PROXY_PORT=9' \
    'Environment=XDG_CACHE_HOME=/run/asterinas-physical-home/.cache' \
    >"$_asterinas_control/$_asterinas_browser.d/physical.conf" || \
    record_failure $? write-browser-drop-in
```

Replace the existing drop-in `printf` rather than creating a second one. Extend the final action `case` with only:

```sh
daily-use)
    shift
    daily_use "$@"
    ;;
```

Do not add any host-provided URL, evidence path, mode, Xorg PID, or executable path.

- [ ] **Step 4: Package the uploader with deterministic ownership and mode**

In `build_stage1.sh`, install `browser_daily_use_upload.py` beside the existing daily-use gate. Its archive path is:

```text
usr/lib/asterinas/browser-daily-use-upload
```

Stage1 exposes it at runtime as `/run/asterinas-tools/browser-daily-use-upload`. Use the same root ownership, `0o755` mode, timestamp normalization, and archive ordering as the existing Python gate tools. Ensure its adjacent import of `browser_daily_use_contract` resolves in Stage1.

- [ ] **Step 5: Run rootfs and shell tests**

```bash
python3 -m unittest tools.riscv.tests.test_debian_rootfs -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit the guest action**

```bash
git add tools/riscv/debian/rootfs/physical_graphics_control.sh \
  tools/riscv/debian/rootfs/physical_external_services_quiesce.sh \
  tools/riscv/debian/rootfs/build_stage1.sh \
  tools/riscv/tests/test_debian_rootfs.py
git commit -m "Wire daily-use profiling into Stage1"
```

### Task 4: Host serial operation for the fixed action

**Files:**
- Modify: `tools/riscv/megrez_physical_graphics.py`
- Modify: `tools/riscv/tests/test_megrez_physical_graphics.py`

- [ ] **Step 1: Write failing parser and serial-operation tests**

Add a frozen result type expectation and tests for exact ID/status agreement:

```python
def test_run_daily_use_profile_requires_matching_terminal_id(self):
    operations = self.real_operations_with_serial(
        b"__ASTERINAS_PHYSICAL_DAILY_USE__ experiment_id="
        b"ffffffffffffffffffffffffffffffff outcome=pass gate_status=0 upload_status=0\n"
    )
    with self.assertRaisesRegex(self.module.HostGateError, "experiment"):
        operations.run_daily_use_profile(EXPERIMENT_ID, 120.0, 4242)

def test_run_daily_use_profile_returns_closed_terminal_status(self):
    operations = self.real_operations_with_serial(valid_daily_use_terminal())
    status = operations.run_daily_use_profile(EXPERIMENT_ID, 120.0, 4242)
    self.assertEqual(status.experiment_id, EXPERIMENT_ID)
    self.assertEqual(status.outcome, "pass")
    self.assertEqual((status.gate_status, status.upload_status), (0, 0))
```

Also reject duplicate terminal markers, non-decimal/negative statuses, unknown outcomes, nonzero status with `pass`, zero gate status with `fail`, malformed IDs, and trailing fields. Assert the emitted shell command is exactly:

```text
/run/asterinas-tools/physical-graphics-control daily-use 0123456789abcdef0123456789abcdef 120 4242
```

- [ ] **Step 2: Run the focused host operation tests and verify failure**

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_physical_graphics.PhysicalCommandTests.test_daily_use_command_is_closed \
  tools.riscv.tests.test_megrez_physical_graphics.PhysicalCommandTests.test_run_daily_use_profile_requires_matching_terminal_id \
  tools.riscv.tests.test_megrez_physical_graphics.PhysicalCommandTests.test_run_daily_use_profile_returns_closed_terminal_status -v
```

Expected: failure because `DailyUseTerminalStatus` and `run_daily_use_profile` are absent.

- [ ] **Step 3: Add the typed terminal parser and operation**

Add:

```python
@dataclass(frozen=True)
class DailyUseTerminalStatus:
    experiment_id: str
    outcome: str
    gate_status: int
    upload_status: int

def physical_daily_use_command(
    experiment_id: str, timeout_seconds: float, expected_firefox_pid: int
) -> str:
    if re.fullmatch(r"[0-9a-f]{32}", experiment_id) is None:
        raise HostGateError("daily-use experiment identity is invalid")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 1 <= timeout_seconds <= 120
        or int(timeout_seconds) != timeout_seconds
    ):
        raise HostGateError("daily-use timeout is invalid")
    if type(expected_firefox_pid) is not int or expected_firefox_pid <= 0:
        raise HostGateError("daily-use Firefox PID is invalid")
    timeout = int(timeout_seconds)
    return (
        "/run/asterinas-tools/physical-graphics-control daily-use "
        f"{experiment_id} {timeout} {expected_firefox_pid}"
    )
```

`RealPhysicalGraphicsOperations.run_daily_use_profile()` must use the same bounded serial command primitive as `run_cycle`, search only output emitted after the command, require one exact marker, validate all fields, and return `DailyUseTerminalStatus`. It must not reboot, publish, or retry the profile.

- [ ] **Step 4: Run all physical graphics host tests**

```bash
python3 -m unittest tools.riscv.tests.test_megrez_physical_graphics -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit the reusable operation**

```bash
git add tools/riscv/megrez_physical_graphics.py \
  tools/riscv/tests/test_megrez_physical_graphics.py
git commit -m "Add fixed physical daily-use operation"
```

### Task 5: Dedicated physical daily-use runner

**Files:**
- Create: `tools/riscv/megrez_firefox_daily_use.py`
- Create: `tools/riscv/tests/test_megrez_firefox_daily_use.py`

- [ ] **Step 1: Write failing lifecycle and qualification tests**

Define a fake operations object recording calls and a fake fixture exposing one payload. Add these tests:

```python
def test_successful_run_has_exact_phase_order_and_qualified_result(self):
    operations = FakeOperations(bundle=valid_pass_bundle())
    result = self.module.run_firefox_daily_use(
        self.plan, self.config, operations,
        experiment_id=EXPERIMENT_ID,
        fixture_factory=operations.fixture_factory,
    )
    self.assertTrue(result.passed)
    self.assertTrue(result.qualified)
    self.assertTrue(result.recovered)
    self.assertEqual(
        operations.calls,
        ["invalidate", "fixture-start", "open", "ensure-artifacts", "boot",
         "graphical-readiness", "daily-use", "reboot", "recovery",
         "publish", "fixture-close", "close"],
    )

def test_http_receipt_without_matching_terminal_is_failure(self):
    operations = FakeOperations(
        bundle=valid_pass_bundle(), terminal=terminal(outcome="fail", gate_status=1)
    )
    result = self.run(operations)
    self.assertFalse(result.passed)
    self.assertFalse(result.qualified)

def test_failure_still_attempts_bounded_recovery_and_publishes(self):
    operations = FakeOperations(bundle=None, daily_use_error=TimeoutError("profile"))
    result = self.run(operations)
    self.assertFalse(result.passed)
    self.assertIn("reboot", operations.calls)
    self.assertIn("recovery", operations.calls)
    self.assertIn("publish", operations.calls)
    self.assertEqual(operations.calls[-1], "close")
```

Add tests for no upload, malformed upload, experiment mismatch, gate run mismatch, duplicate output directory, failed 7/7 functions, changed Firefox/Xorg start times, missing sampler coverage, upload/terminal disagreement, failed U-Boot recovery, fixture wrong peer, Ctrl-C/BaseException cleanup, and private atomic publication. Add a prepare-only test proving the serial device is never opened and the printed command contains the stable by-id device plus exact plan/output/timeout arguments.

- [ ] **Step 2: Run the new test module and verify import failure**

```bash
python3 -m unittest tools.riscv.tests.test_megrez_firefox_daily_use -v
```

Expected: `ERROR` with `ModuleNotFoundError`.

- [ ] **Step 3: Define the runner's closed configuration and result types**

Use these public types:

```python
@dataclass(frozen=True)
class DailyUsePhysicalConfig:
    device: Path
    output_directory: Path
    fixture_bind: str = "10.100.19.216"
    fixture_port: int = 17894
    board_peer: str = "10.100.19.200"
    profile_timeout: float = 120.0
    open_timeout: float = 60.0
    artifact_timeout: float = 300.0
    boot_timeout: float = 300.0
    upload_timeout: float = 30.0
    recovery_timeout: float = 930.0

@dataclass(frozen=True)
class DailyUsePhysicalResult:
    schema_version: int
    experiment_id: str
    gate_run_id: str | None
    passed: bool
    qualified: bool
    reason: str
    recovered: bool
    plan_sha256: str
    bootargs_sha256: str
    fixture: dict[str, object]
    deployment: dict[str, object]
    terminal: dict[str, object] | None
    artifacts: tuple[dict[str, object], ...]
```

Define a `DailyUsePhysicalOperations` protocol containing the existing public physical operations plus `run_daily_use_profile`, `publish_daily_use`, and `close`. The real adapter must compose `RealPhysicalGraphicsOperations`; do not duplicate U-Boot transfer, graphical readiness, debug console, browser startup, reboot, or recovery logic.

Add `daily_use_physical_bootargs(plan)` beside `physical_bootargs(plan)`. Preserve the latter unchanged. The daily-use form uses the same console, log, reboot, service masks, root-init, isolated debug console, and volatile home, plus these frozen tokens:

```text
asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1
asterinas.neighbor=eic7700-rj45,10.100.19.216,04:7c:16:47:50:4e
systemd.setenv=ASTERINAS_PHYSICAL_DAILY_USE=1
systemd.setenv=ASTERINAS_DESKTOP_FIXTURE_URL=http://10.100.19.216:17894/asterinas-network-probe.bin
systemd.setenv=ASTERINAS_BROWSER_WEB_BASIC_ONLY=1
systemd.setenv=ASTERINAS_WEB_NETWORK_MODE=proxy
systemd.setenv=ASTERINAS_DESKTOP_PROXY_HOST=10.100.19.216
systemd.setenv=ASTERINAS_DESKTOP_PROXY_PORT=9
```

Add a unit test proving the normal physical bootargs contain none of the daily-use network tokens and the daily-use form contains each exactly once.

Expose this lifecycle entry point and result builder:

```python
def run_firefox_daily_use(
    plan: DebugPlan,
    config: DailyUsePhysicalConfig,
    operations: DailyUsePhysicalOperations,
    *,
    experiment_id: str | None = None,
    fixture_factory: Callable[[FixtureConfig], FixtureServer] = FixtureServer,
    artifact_validator: Callable[[DebugPlan], object] = _validate_physical_artifacts,
) -> DailyUsePhysicalResult:
    """Run one boot and one profile, always attempting recovery and publication."""

def _build_result(
    plan: DebugPlan,
    bootargs: str,
    experiment_id: str,
    terminal: DailyUseTerminalStatus | None,
    bundle: EvidenceBundle | None,
    recovered: bool,
    failure: BaseException | None,
) -> DailyUsePhysicalResult:
    """Derive pass and qualification only from validated cross-channel evidence."""
```

- [ ] **Step 4: Implement lifecycle, cross-channel agreement, and private publication**

Implement this exact phase order in `run_firefox_daily_use`; all variables used by cleanup are initialized before the first external operation:

```python
selected_id = experiment_id or secrets.token_hex(16)
plan.validate()
artifact_validator(plan)
bootargs = daily_use_physical_bootargs(plan)
fixture = fixture_factory(
    FixtureConfig(
        bind_address=config.fixture_bind,
        port=config.fixture_port,
        allowed_peer=config.board_peer,
        daily_use_experiment_id=selected_id,
    )
)
terminal = None
bundle = None
transport = ()
failure = None
interruption = None
recovered = False
try:
    try:
        operations.invalidate()
        fixture.start()
        operations.open(config.open_timeout)
        transport = operations.ensure_artifacts(plan, config.artifact_timeout)
        operations.boot(plan, bootargs, config.boot_timeout)
        readiness = operations.prove_graphical_readiness(config.boot_timeout)
        terminal = operations.run_daily_use_profile(
            selected_id, config.profile_timeout, readiness.browser_pid
        )
        raw_bundle = fixture.wait_for_daily_use_evidence(config.upload_timeout)
        bundle = parse_bundle(raw_bundle, selected_id)
    except Exception as error:
        failure = error
    except BaseException as error:
        interruption = error

    if operations.guest_started:
        try:
            operations.request_reboot(min(config.recovery_timeout, 30.0))
        except Exception:
            pass
        except BaseException as error:
            if interruption is None:
                interruption = error
        try:
            operations.await_recovery(config.recovery_timeout)
            recovered = True
        except Exception as error:
            if failure is None:
                failure = error
            else:
                failure = HostGateError(
                    f"{_failure_reason(failure)}; recovery={_failure_reason(error)}"
                )
        except BaseException as error:
            if interruption is None:
                interruption = error

    if interruption is not None and failure is None:
        failure = HostGateError("physical daily-use run was interrupted")
    result = _build_result(
        plan, bootargs, selected_id, terminal, bundle, recovered, failure
    )
    operations.publish_daily_use(
        result, bundle, operations.transcript, transport,
        fixture.daily_use_evidence_summary(),
    )
    if interruption is not None:
        raise interruption
    return result
finally:
    try:
        fixture.close()
    finally:
        operations.close()
```

Wrap fixture closure and operation closure so both are independently attempted. A recovery failure is retained alongside an earlier primary failure in `reason`. Re-raise `KeyboardInterrupt` and `SystemExit` only after recovery and publication, matching the existing physical runner policy.

Qualification requires all of these booleans: terminal pass with both statuses zero; parsed pass bundle; validated daily-use result state pass; seven function groups pass; unchanged Firefox and Xorg PID/start-time snapshots; both `systemArtifact` and `threadArtifact` present in attribution; sampler coverage spans the workload; matching experiment ID; matching gate run ID; recovered U-Boot prompt. Store every failed predicate in the canonical result reason.

Create the output directory with mode `0o700` and exclusive creation. Publish each artifact using held-directory `openat`/`O_EXCL|O_NOFOLLOW`, mode `0o600`, `fsync` file and directory, then publish `serial.log`, `fixture-summary.json`, `deployment.json`, `run-result.json`, and `sha256-manifest.json`. The manifest covers every previously published file and is written last. A failed run uses the same result/manifest rules but can never set `qualified: true`.

- [ ] **Step 5: Add CLI and prepare-only behavior**

The CLI accepts:

```text
DEVICE --plan PLAN --output-directory DIR
--fixture-bind 10.100.19.216 --fixture-port 17894 --board-peer 10.100.19.200
--profile-timeout 120 --open-timeout 60 --artifact-timeout 300
--boot-timeout 300 --upload-timeout 30 --recovery-timeout 930
--mmc-kernel PATH --mmc-initramfs PATH --mmc-dtb PATH
--prepare-only
```

Require either all three MMC paths or none. `--prepare-only` validates the plan and immutable artifacts, validates that the output path does not exist, prints the shell-escaped non-prepare command, and returns without opening a fixture socket or serial device.

- [ ] **Step 6: Run runner and reused lifecycle tests**

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_firefox_daily_use \
  tools.riscv.tests.test_megrez_physical_graphics -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit the physical runner**

```bash
git add tools/riscv/megrez_firefox_daily_use.py \
  tools/riscv/tests/test_megrez_firefox_daily_use.py
git commit -m "Add physical Firefox daily-use runner"
```

### Task 6: Three-run report and conservative mechanism classification

**Files:**
- Create: `tools/riscv/megrez_firefox_daily_use_report.py`
- Create: `tools/riscv/tests/test_megrez_firefox_daily_use_report.py`

- [ ] **Step 1: Write failing qualification and classification tests**

Create synthetic run directories using the exact seven-artifact pass bundle. Add tests asserting:

```python
def test_report_requires_three_distinct_qualified_runs(self):
    with self.assertRaisesRegex(ReportError, "three distinct"):
        build_report([self.run_a, self.run_b, self.run_b])

def test_runnable_delay_requires_recurrence_in_all_runs(self):
    runs = [self.make_run(wait_ratio=0.30) for _ in range(3)]
    report = build_report(runs)
    self.assertEqual(report["classification"], "runnable-delayed")
    self.assertEqual(report["classificationAgreement"], 3)

def test_one_outlier_cannot_admit_a_mechanism(self):
    runs = [self.make_run(wait_ratio=value) for value in (0.30, 0.04, 0.03)]
    report = build_report(runs)
    self.assertEqual(report["classification"], "mixed")
```

Add boundary tests for executing, sleeping-blocking, mixed, incomplete coverage, artifact hash mismatch, repeated experiment/gate IDs, mixed commit/artifact hashes, unstable fixture/display/board/topology identities, and a category that crosses its diagnostic threshold in only one run. The last case must show that a threshold flag is reported but does not override the recurring attribution ratios.

- [ ] **Step 2: Run the new report tests and verify import failure**

```bash
python3 -m unittest tools.riscv.tests.test_megrez_firefox_daily_use_report -v
```

Expected: `ERROR` with `ModuleNotFoundError`.

- [ ] **Step 3: Implement immutable input verification and raw metric extraction**

For each input directory, verify `sha256-manifest.json` before parsing any JSON. Require `run-result.json` to be passed, qualified, and recovered. Require distinct experiment and gate IDs but identical commit, kernel, Stage1, DTB, rootfs-manifest, board serial, hart count, fixture, display provider, browser package, and bootargs identities.

Extract and retain per run:

```python
PRIMARY_METRICS = (
    "keyboardFirstRafP95Ms",
    "keyboardNextRafP95Ms",
    "pointerFirstRafP95Ms",
    "pointerNextRafP95Ms",
    "scrollFirstRafP95Ms",
    "scrollNextRafP95Ms",
    "navigationCommandMs",
    "navigationResponseToDomMs",
    "contextSwitchTotalMs",
)
ATTRIBUTION_METRICS = (
    "firefoxCpuSeconds",
    "xorgCpuSeconds",
    "mainThreadRuntimeSeconds",
    "mainThreadRunqueueWaitSeconds",
    "mainThreadDispatches",
    "systemUtilization",
    "contextSwitches",
    "runnableSamples",
)
```

Expose this exact API:

```python
class ReportError(ValueError):
    pass

def build_report(run_directories: Sequence[Path]) -> dict[str, object]:
    """Validate and summarize exactly three immutable qualified runs."""

def main(argv: list[str] | None = None) -> int:
    """Write exclusive canonical JSON and Markdown reports."""
```

Also retain every raw keyboard, pointer, scroll, navigation, context duration, phase duration, process sample, and thread sample in the report's `runs` array. For every scalar metric produce `{values, median, minimum, maximum}` using `statistics.median`; never discard an outlier.

- [ ] **Step 4: Implement predeclared classifier thresholds**

Classify each run from the affected phase and its hottest Firefox thread using these rules in order:

```python
wait_ratio = main_runqueue_wait / max(main_runtime + main_runqueue_wait, 1e-9)
main_runtime_share = main_runtime / max(total_firefox_runtime, 1e-9)
process_cpu_occupancy = total_firefox_runtime / max(profile_wall_seconds, 1e-9)
unaccounted_ratio = max(profile_wall_seconds - total_firefox_runtime, 0.0) / max(profile_wall_seconds, 1e-9)

if wait_ratio >= 0.20 and main_runqueue_wait * 1000 >= affected_primary_ms:
    classification = "runnable-delayed"
elif main_runtime_share >= 0.50 and process_cpu_occupancy >= 0.50 and wait_ratio < 0.20:
    classification = "executing"
elif unaccounted_ratio >= 0.50 and process_cpu_occupancy < 0.25 and wait_ratio < 0.20:
    classification = "sleeping-blocking"
else:
    classification = "mixed"
```

The cross-run classification is non-`mixed` only when all three runs have the same classification and the hottest thread is the Firefox process leader (`tid == firefox pid`) in every run. Otherwise emit `mixed`. Report existing daily-use diagnostic-threshold crossings independently; they are signals and never override the attribution ratios. Include every threshold and intermediate ratio in output so the decision is reproducible. Describe this as a mechanism-class admission rule, not proof of a specific function or subsystem.

- [ ] **Step 5: Add canonical JSON and Markdown output**

CLI:

```text
python3 -m tools.riscv.megrez_firefox_daily_use_report RUN_A RUN_B RUN_C \
  --json-output target/firefox-daily-use-physical/baseline-report.json \
  --markdown-output docs/performance/2026-09-18-firefox-daily-use-physical-baseline.md
```

Create output files exclusively with mode `0o600`. JSON is canonical. Markdown includes: scope, immutable identities, one table of raw per-run primary metrics, median/min/max, attribution values and ratios, classification, limitations, hashes/paths, and the admission decision. It must say that browser timings are not USB-to-HDMI latency and procfs placeholder fault fields were not used.

- [ ] **Step 6: Run report tests**

```bash
python3 -m unittest tools.riscv.tests.test_megrez_firefox_daily_use_report -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit the analyzer**

```bash
git add tools/riscv/megrez_firefox_daily_use_report.py \
  tools/riscv/tests/test_megrez_firefox_daily_use_report.py
git commit -m "Add physical daily-use baseline report"
```

### Task 7: Developer entry points and operator documentation

**Files:**
- Modify: `Makefile`
- Modify: `tools/riscv/firefox_fast_check.sh`
- Modify: `tools/riscv/README.md`
- Modify: `tools/riscv/tests/test_megrez_firefox_daily_use.py`

- [ ] **Step 1: Write failing Makefile, fast-check, and README contract tests**

Assert `test_riscv_firefox_daily_use_physical_unit` runs all four relevant new modules and existing daily-use modules. Assert `prepare_riscv_megrez_firefox_daily_use` requires plan, stable by-id device, output, fixture bind/peer, validates the plan, and prints rather than executes `tools.riscv.megrez_firefox_daily_use`. Assert the README contains the exact three-run directory layout and explicitly says one profile per boot.

- [ ] **Step 2: Run contract tests and verify failure**

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_firefox_daily_use \
  tools.riscv.tests.test_debian_rootfs -v
```

Expected: contract-test failures for absent Make targets and documentation.

- [ ] **Step 3: Add Make targets with fail-closed inputs**

Add:

```make
.PHONY: test_riscv_firefox_daily_use_physical_unit
test_riscv_firefox_daily_use_physical_unit:
	@PYTHONPATH="$(CURDIR)" python3 -m unittest \
		tools.riscv.tests.test_browser_daily_use_contract \
		tools.riscv.tests.test_browser_daily_use_gate \
		tools.riscv.tests.test_browser_daily_use_upload \
		tools.riscv.tests.test_megrez_network_fixture \
		tools.riscv.tests.test_debian_rootfs \
		tools.riscv.tests.test_megrez_physical_graphics \
		tools.riscv.tests.test_megrez_firefox_daily_use \
		tools.riscv.tests.test_megrez_firefox_daily_use_report -v
```

Add `prepare_riscv_megrez_firefox_daily_use` with variables `MEGREZ_FIREFOX_DAILY_USE_PLAN`, `MEGREZ_FIREFOX_DAILY_USE_DEVICE`, `MEGREZ_FIREFOX_DAILY_USE_OUTPUT`, `MEGREZ_FIREFOX_DAILY_USE_FIXTURE_BIND`, and `MEGREZ_FIREFOX_DAILY_USE_BOARD_PEER`. Require an absolute `/dev/serial/by-id/` device, output not already present, fixture bind `10.100.19.216`, peer `10.100.19.200`, and the all-or-none MMC triple. Its final recipe invokes the runner with `--prepare-only`; the runner prints the exact real command.

- [ ] **Step 4: Extend the fast check and README**

Add the new test modules to `tools/riscv/firefox_fast_check.sh`. Document:

1. Docker build of Sv39/SMP=4 kernel.
2. Deterministic Stage1 build.
3. Artifact/plan validation without serial access.
4. Host Ethernet and board peer preflight.
5. Three distinct one-profile-per-boot commands with `run-a1`, `run-a2`, `run-a3` output directories.
6. Expected U-Boot recovery after every run.
7. Report generation and the rule that only qualified runs are inputs.

Use the stable device `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`, fixture `10.100.19.216:17894`, board peer `10.100.19.200`, and `target/firefox-daily-use-physical/` paths in the examples.

- [ ] **Step 5: Run the short regression gate**

```bash
tools/riscv/firefox_fast_check.sh
```

Expected: all tests pass; the count is greater than the pre-change 354-test baseline.

- [ ] **Step 6: Commit entry points and docs**

```bash
git add Makefile tools/riscv/firefox_fast_check.sh tools/riscv/README.md \
  tools/riscv/tests/test_megrez_firefox_daily_use.py
git commit -m "Document physical daily-use baseline workflow"
```

### Task 8: Host verification, Stage1 audit, and QEMU non-physical smoke

**Files:**
- Modify only if a failing verification exposes a defect in files from Tasks 1-7.

- [ ] **Step 1: Run Python compile and unit verification**

```bash
python3 -m compileall -q \
  tools/riscv/debian/rootfs/browser_daily_use_upload.py \
  tools/riscv/megrez_firefox_daily_use.py \
  tools/riscv/megrez_firefox_daily_use_report.py
make test_riscv_firefox_daily_use_physical_unit
tools/riscv/firefox_fast_check.sh
```

Expected: exit 0 for every command.

- [ ] **Step 2: Build and audit deterministic Stage1 twice**

```bash
mkdir -p target/firefox-daily-use-physical/stage1-a \
  target/firefox-daily-use-physical/stage1-b
tools/docker/run_dev_container.sh \
  --image asterinas/asterinas:0.18.0-20260702-riscv-rootfs --offline -- \
  tools/riscv/debian/rootfs/build_stage1.sh \
  target/firefox-daily-use-physical/stage1-a/initramfs.cpio
tools/docker/run_dev_container.sh \
  --image asterinas/asterinas:0.18.0-20260702-riscv-rootfs --offline -- \
  tools/riscv/debian/rootfs/build_stage1.sh \
  target/firefox-daily-use-physical/stage1-b/initramfs.cpio
sha256sum \
  target/firefox-daily-use-physical/stage1-a/initramfs.cpio \
  target/firefox-daily-use-physical/stage1-b/initramfs.cpio
```

Expected: both SHA-256 values are identical. Inspect the archive and require one executable `usr/lib/asterinas/browser-daily-use-upload` and the existing gate/contract files.

- [ ] **Step 3: Build the current RISC-V Sv39/SMP=4 kernel in the persistent container**

```bash
tools/docker/run_dev_container.sh --workspace \
  /home/ubuntu/.config/superpowers/worktrees/asterinas/firefox-daily-use-perf -- \
  make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
```

Expected: exit 0 and current kernel artifacts produced from the isolated worktree commit.

- [ ] **Step 4: Run the relevant four-hart QEMU smoke without treating it as physical evidence**

Run:

```bash
ASTERINAS_SHARED_ARTIFACTS=/home/ubuntu/xaj/Program/asterinas/target
make test_riscv_physical_graphics_qemu_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$ASTERINAS_SHARED_ARTIFACTS/qemu-uboot/cache/u-boot-build/u-boot" \
  DEBIAN_DTB="$ASTERINAS_SHARED_ARTIFACTS/qemu-uboot/current/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/firefox-daily-use-physical/stage1-a/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$ASTERINAS_SHARED_ARTIFACTS/debian-riscv/desktop-m5-usable/rootfs/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$ASTERINAS_SHARED_ARTIFACTS/debian-riscv/desktop-m5-usable/rootfs/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$ASTERINAS_SHARED_ARTIFACTS/debian-riscv/desktop-m5-usable/rootfs/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$ASTERINAS_SHARED_ARTIFACTS/debian-riscv/desktop-m5-usable/rootfs/source-metadata/package-checksums" \
  RISCV_PHYSICAL_GRAPHICS_QEMU_GATE_OUTPUT="$PWD/target/firefox-daily-use-physical/qemu-smoke"
```

Expected: functional/browser orchestration smoke passes. Record its command and hashes in the verification log, explicitly labeled `non-physical`.

- [ ] **Step 5: Repair only observed verification failures with a failing regression first**

For each failure, add one test reproducing the exact output, run it to observe failure, make the smallest correction in the owning file, and rerun Steps 1-4. Do not change kernel behavior during this task.

- [ ] **Step 6: Commit any verification-only correction**

If no correction was needed, do not create an empty commit. Otherwise:

```bash
git add tools/riscv/debian/rootfs/browser_daily_use_upload.py \
  tools/riscv/debian/rootfs/physical_graphics_control.sh \
  tools/riscv/debian/rootfs/physical_external_services_quiesce.sh \
  tools/riscv/debian/rootfs/build_stage1.sh \
  tools/riscv/megrez_network_fixture.py \
  tools/riscv/megrez_physical_graphics.py \
  tools/riscv/megrez_firefox_daily_use.py \
  tools/riscv/megrez_firefox_daily_use_report.py \
  tools/riscv/tests/test_browser_daily_use_upload.py \
  tools/riscv/tests/test_megrez_network_fixture.py \
  tools/riscv/tests/test_debian_rootfs.py \
  tools/riscv/tests/test_megrez_physical_graphics.py \
  tools/riscv/tests/test_megrez_firefox_daily_use.py \
  tools/riscv/tests/test_megrez_firefox_daily_use_report.py
git commit -m "Fix physical daily-use verification defect"
```

### Task 9: Prepare immutable physical inputs

**Files:**
- Create generated evidence only under: `target/firefox-daily-use-physical/`

- [ ] **Step 1: Record the source and artifact identity set**

```bash
mkdir -p target/firefox-daily-use-physical/identities
git rev-parse HEAD
ASTERINAS_SHARED_ARTIFACTS=/home/ubuntu/xaj/Program/asterinas/target
ASTERINAS_PLAN_BOOTARGS='console=tty0 console=ttyS0 cpu_no_boost_1_6ghz loglevel=info init=/init asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1 asterinas.reboot_after=600 -- --root-init=systemd'
python3 -m tools.riscv.megrez_debug plan \
  --profile debian-browser \
  --kernel "$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  --initramfs "$PWD/target/firefox-daily-use-physical/stage1-a/initramfs.cpio" \
  --qemu-dtb "$ASTERINAS_SHARED_ARTIFACTS/qemu-uboot/current/qemu-virt.dtb" \
  --megrez-dtb "$ASTERINAS_SHARED_ARTIFACTS/megrez-browser-network/tftp/eic7700-milkv-megrez.dtb" \
  --u-boot "$ASTERINAS_SHARED_ARTIFACTS/qemu-uboot/cache/u-boot-build/u-boot" \
  --root-image "$ASTERINAS_SHARED_ARTIFACTS/debian-riscv/desktop-m5-usable/rootfs/debian-root.ext2" \
  --root-manifest "$ASTERINAS_SHARED_ARTIFACTS/debian-riscv/desktop-m5-usable/rootfs/rootfs-manifest.json" \
  --packages-lock "$ASTERINAS_SHARED_ARTIFACTS/debian-riscv/desktop-m5-usable/rootfs/packages.lock" \
  --package-checksums "$ASTERINAS_SHARED_ARTIFACTS/debian-riscv/desktop-m5-usable/rootfs/source-metadata/package-checksums" \
  --in-release "$ASTERINAS_SHARED_ARTIFACTS/debian-riscv/desktop-m5-usable/rootfs/source-metadata/InRelease" \
  --bootargs "$ASTERINAS_PLAN_BOOTARGS" --paging-mode sv39 --reboot-after 600 \
  --output target/firefox-daily-use-physical/plan.json
python3 -m tools.riscv.megrez_debug check \
  target/firefox-daily-use-physical/plan.json
sha256sum \
  target/osdk/aster-kernel/aster-kernel-osdk-bin.Image \
  target/firefox-daily-use-physical/stage1-a/initramfs.cpio \
  "$ASTERINAS_SHARED_ARTIFACTS/qemu-uboot/current/qemu-virt.dtb" \
  "$ASTERINAS_SHARED_ARTIFACTS/megrez-browser-network/tftp/eic7700-milkv-megrez.dtb" \
  "$ASTERINAS_SHARED_ARTIFACTS/qemu-uboot/cache/u-boot-build/u-boot" \
  "$ASTERINAS_SHARED_ARTIFACTS/debian-riscv/desktop-m5-usable/rootfs/rootfs-manifest.json"
```

Expected: the commit is on `codex/firefox-daily-use-perf`; plan creation and validation pass; every listed input has one SHA-256. The shared worktree is used only as a read-only source of already frozen U-Boot, DTBs, and rootfs artifacts.

- [ ] **Step 2: Verify host interface and idle serial ownership read-only**

```bash
ip -brief address show
readlink -f /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0
fuser /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0 || true
```

Expected: host Ethernet includes `10.100.19.216/21`, the stable serial link resolves, and no unrelated process owns it. If another process owns the device, stop before opening serial and report the PID/command.

- [ ] **Step 3: Run prepare-only validation for each exclusive output**

```bash
for run in a1 a2 a3; do
  make prepare_riscv_megrez_firefox_daily_use \
    MEGREZ_FIREFOX_DAILY_USE_PLAN=target/firefox-daily-use-physical/plan.json \
    MEGREZ_FIREFOX_DAILY_USE_DEVICE=/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0 \
    MEGREZ_FIREFOX_DAILY_USE_OUTPUT=target/firefox-daily-use-physical/run-$run \
    MEGREZ_FIREFOX_DAILY_USE_FIXTURE_BIND=10.100.19.216 \
    MEGREZ_FIREFOX_DAILY_USE_BOARD_PEER=10.100.19.200
done | tee target/firefox-daily-use-physical/identities/prepared-commands.txt
chmod 0600 target/firefox-daily-use-physical/identities/prepared-commands.txt
```

Expected: three shell-escaped commands are printed; no output run directory is created and the serial device remains unopened.

- [ ] **Step 4: Save the exact commands and identity output**

Verify `prepared-commands.txt` contains exactly three commands with distinct output directories and identical plan, serial, fixture, peer, and timeouts. Record its SHA-256. The runner records commit, artifact hashes, board serial path, host/peer addresses, hart count, display provider, and browser package identity in each run's canonical `deployment.json`; compare those files byte-for-byte except for the explicitly run-specific IDs and output path.

### Task 10: Execute three current-kernel physical profiles

**Files:**
- Create generated evidence only under: `target/firefox-daily-use-physical/run-a1/`, `run-a2/`, and `run-a3/`

- [ ] **Step 1: Execute run A1 and inspect qualification before proceeding**

Execute the exact command printed for `run-a1` in Task 9.

Expected: one boot, one profile, terminal pass, one matching upload, seven function groups pass, stable Firefox/Xorg identities, valid sampler coverage, and bounded recovery to the U-Boot prompt. Confirm `run-result.json` has `passed`, `qualified`, and `recovered` all `true`; confirm the manifest verifies every published file.

- [ ] **Step 2: Execute run A2 from a fresh boot**

Execute the exact `run-a2` command only after A1 has recovered and released the serial device.

Expected: the same qualification conditions, with distinct experiment and gate run IDs but identical immutable deployment identities.

- [ ] **Step 3: Execute run A3 from a fresh boot**

Execute the exact `run-a3` command only after A2 has recovered and released the serial device.

Expected: the same qualification conditions, again with distinct run identities and identical immutable deployment identities.

- [ ] **Step 4: Handle a failed attempt without replacing evidence**

If an attempt is unqualified, preserve its exclusive directory and classify the reason. Correct only an infrastructure defect proven by its evidence, rerun host tests, and use a new monotonic directory such as `run-a4`; never delete, overwrite, or count the failed attempt. Continue until three qualified runs exist under the same immutable identity set.

- [ ] **Step 5: Verify all manifests independently**

Use `sha256sum -c` from each qualified run directory and re-run `parse_bundle`/`validate_daily_use_result` through the report tool.

Expected: every hash and contract validates; there are three distinct experiment IDs and three distinct gate IDs.

### Task 11: Publish baseline and select the Phase 2 path

**Files:**
- Create: `docs/performance/2026-09-18-firefox-daily-use-physical-baseline.md`
- Create generated report: `target/firefox-daily-use-physical/baseline-report.json`

- [ ] **Step 1: Generate the report from exactly three qualified runs**

```bash
python3 -m tools.riscv.megrez_firefox_daily_use_report \
  target/firefox-daily-use-physical/run-a1 \
  target/firefox-daily-use-physical/run-a2 \
  target/firefox-daily-use-physical/run-a3 \
  --json-output target/firefox-daily-use-physical/baseline-report.json \
  --markdown-output docs/performance/2026-09-18-firefox-daily-use-physical-baseline.md
```

Expected: exit 0; Markdown and JSON agree on raw values, median/range, identities, and classification.

- [ ] **Step 2: Audit every narrative claim against raw evidence**

For each statement in the Markdown report, identify the exact artifact field and denominator. Confirm that thresholds are described as diagnostic, browser timings are not described as end-to-end display latency, and no causal subsystem/function claim exceeds the classifier's mechanism class.

- [ ] **Step 3: Apply the optimization-admission decision**

If all three runs classify as `executing`, `runnable-delayed`, or `sleeping-blocking`, create a new dated design and implementation plan for one focused regression and one change targeting only that repeated mechanism. If the report classifies as `mixed`, create a new dated bounded-attribution design that adds only the missing observation (sampled PC is eligible only when instruction-region attribution is the missing boundary), measures its overhead, and repeats the same workload before choosing a kernel change.

The Phase 2 plan must prescribe three A and three B physical boots, alternating variants when practical, and enforce the approved speedup criteria. It must not claim improvement from the Phase 1 baseline alone.

- [ ] **Step 4: Run final regression and repository checks**

```bash
tools/riscv/firefox_fast_check.sh
tools/docker/run_dev_container.sh --workspace \
  /home/ubuntu/.config/superpowers/worktrees/asterinas/firefox-daily-use-perf -- \
  make check
git status --short
```

Expected: tests/checks pass; status shows only the intended baseline report if it has not yet been committed and generated `target/` evidence remains ignored.

- [ ] **Step 5: Commit the audited baseline report**

```bash
git add docs/performance/2026-09-18-firefox-daily-use-physical-baseline.md
git commit -m "Record physical Firefox daily-use baseline"
```

The commit message and report must describe evidence and classification, not a speedup.
