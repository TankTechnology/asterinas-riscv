# Megrez Lightweight Browser Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bounded QEMU browser-usability gate and one simulation-guarded
Megrez acceptance run for lightweight NetSurf browsing.

**Architecture:** Extend the restricted Megrez fixture with deterministic Web
resources and a bounded XWD upload endpoint. Add one M8 guest driver after M7,
one QEMU adapter that captures each visual state, and a schema-3 Megrez plan
that reuses the same ordered markers. QEMU is the exhaustive gate; physical
hardware receives one high-information run only after exact artifacts pass.

**Tech Stack:** Python 3 `unittest`, Bash/systemd, NetSurf 3.11, Xdotool, XWD,
QEMU HMP screenshots, Debian 13 RISC-V, immutable Megrez plans.

---

## File map

- `tools/riscv/megrez_network_fixture.py`: deterministic browser resources,
  bounded screenshot upload, and canonical fixture evidence.
- `tools/riscv/tests/test_megrez_network_fixture.py`: real loopback fixture and
  upload tests.
- `tools/riscv/debian/rootfs/desktop_m8_browser_quality_evidence.sh`: guest X11
  interaction, download verification, soak, and physical screenshot upload.
- `tools/riscv/debian/rootfs/desktop_m8_browser_quality_gate.py`: M8 transcript
  classifier and QEMU screenshot lifecycle.
- `tools/riscv/tests/test_debian_m8_browser_quality.py`: guest and host M8
  protocol tests.
- `tools/riscv/debian/rootfs/profiles.py`: signed `x11-apps` admission for
  `/usr/bin/xwd`.
- `tools/riscv/debian/rootfs/build_rootfs.sh`: M8 script and systemd unit.
- `tools/riscv/megrez_debug_contract.py`: schema-3 browser-quality plan and
  marker identity.
- `tools/riscv/megrez_debug_desktop.py`: plan-bound M8 QEMU invocation and
  evidence validation.
- `tools/riscv/megrez_debug_board.py`: schema-3 pointer-degraded marker path.
- `tools/riscv/tests/test_megrez_debug.py`: schema-3 and physical collector
  state tests.
- `tools/riscv/tests/test_megrez_debug_desktop.py`: immutable M8 simulation
  adapter tests.
- `Makefile`: unit and full QEMU gate targets.
- `tools/riscv/debian/rootfs/README.md`: operator commands and evidence meaning.

### Task 1: Extend the restricted fixture

**Files:**

- Modify: `tools/riscv/megrez_network_fixture.py`
- Modify: `tools/riscv/tests/test_megrez_network_fixture.py`

- [ ] **Step 1: Write failing resource and upload tests**

Add real-loopback tests for these exact paths and identities:

```python
BROWSER_INDEX_PATH = "/browser-quality/index.html"
BROWSER_SECOND_PATH = "/browser-quality/second.html"
BROWSER_IMAGE_PATH = "/browser-quality/pattern.png"
BROWSER_DOWNLOAD_PATH = "/browser-quality/download.bin"
BROWSER_CAPTURE_PATH = "/browser-quality/capture.xwd.gz"
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
```

Assert that the index contains Chinese and Latin text, links to the second page
and download, and a GET form with `name="q"`. Assert exact content types,
lengths, SHA-256 values, `Cache-Control: no-store`, peer allowlisting, and 404
for unknown paths.

Add a POST helper and assert:

```python
status, body = self.post(server, BROWSER_CAPTURE_PATH, b"gzip-xwd")
self.assertEqual((status, body), (201, b""))
self.assertEqual(server.capture_summary(), {
    "bytes": 8,
    "path": BROWSER_CAPTURE_PATH,
    "peer": "127.0.0.1",
    "sha256": hashlib.sha256(b"gzip-xwd").hexdigest(),
})
self.assertEqual(server.capture_payload(), b"gzip-xwd")
```

Reject missing, non-decimal, zero, or oversized `Content-Length`; short bodies,
chunked bodies, a second upload, a denied peer, and any other POST path. Verify
that all rejected requests leave `capture_summary()` equal to `None`.

- [ ] **Step 2: Run RED**

Run:

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_network_fixture -v
```

Expected: FAIL because the browser paths, `do_POST`, and `capture_summary` do
not exist; existing fixed-payload tests remain green.

- [ ] **Step 3: Implement deterministic resources and bounded upload**

Keep the existing fixed payload and schema-1 summary byte-compatible. Add a
separate resource lookup and capture state:

```python
BROWSER_INDEX_PATH = "/browser-quality/index.html"
BROWSER_SECOND_PATH = "/browser-quality/second.html"
BROWSER_IMAGE_PATH = "/browser-quality/pattern.png"
BROWSER_DOWNLOAD_PATH = "/browser-quality/download.bin"
BROWSER_CAPTURE_PATH = "/browser-quality/capture.xwd.gz"
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
BROWSER_DOWNLOAD = bytes(range(256)) * 1024
BROWSER_DOWNLOAD_SHA256 = hashlib.sha256(BROWSER_DOWNLOAD).hexdigest()
BROWSER_IMAGE = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000020000000200806000000737a7af4"
    "000000414944415478da6310cf58f31f19a3035acb338c3a60c01d406f0bd1e547"
    "1d30f00e18cd05a30e18cd05a30e18cd05a30e18cd05a30e18cd05a30e18cd0523"
    "de0100a3694cb594617d3a0000000049454e44ae426082"
)
BROWSER_INDEX = b"""<!doctype html>
<meta charset=utf-8><title>Asterinas Browser Quality</title>
<style>body{font-family:sans-serif}.scroll{height:1600px}
.second{position:absolute;left:40px;top:220px}
.download{position:absolute;left:40px;top:260px}</style>
<h1>Asterinas browser quality / 浏览器质量</h1>
<form method=get><input name=q><button>Search</button></form>
<img src=/browser-quality/pattern.png alt=pattern>
<p class=second><a href=/browser-quality/second.html>Second page</a></p>
<p class=download><a href=/browser-quality/download.bin>Download</a></p>
<div class=scroll></div>"""
BROWSER_SECOND = b"""<!doctype html>
<meta charset=utf-8><title>Second - Asterinas Browser Quality</title>
<h1>Second page / 第二页</h1>
<a href=/browser-quality/index.html>First page</a>"""

def browser_resource(path: str) -> tuple[str, bytes] | None:
    resources = {
        BROWSER_INDEX_PATH: ("text/html; charset=utf-8", BROWSER_INDEX),
        BROWSER_SECOND_PATH: ("text/html; charset=utf-8", BROWSER_SECOND),
        BROWSER_IMAGE_PATH: ("image/png", BROWSER_IMAGE),
        BROWSER_DOWNLOAD_PATH: ("application/octet-stream", BROWSER_DOWNLOAD),
    }
    return resources.get(path.split("?", 1)[0])
```

For the one canonical `?q=asterinas` request, return a separately defined copy
of the index whose title is `asterinas - Asterinas Browser Quality`; reject
duplicate or unknown query fields with 400. The HTML uses fixed inline CSS, a
1600-pixel scroll region, the PNG, a GET form, and reciprocal links. The PNG is
the checked-in 32x32 RGBA checker byte constant above; do not add a runtime
image dependency.

In `Handler`, add `do_POST`; require one exact content length within the cap,
read exactly that many bytes plus a one-byte EOF check, hash once, and commit
capture state under the existing lock. Do not include uploads in the legacy
network request counter. `capture_payload()` returns one detached immutable
`bytes` value or `None`; it never exposes an internal mutable buffer.

- [ ] **Step 4: Run GREEN**

Run the command from Step 2. Expected: all fixture tests pass with no leaked
server threads or sockets.

- [ ] **Step 5: Commit**

```bash
git add tools/riscv/megrez_network_fixture.py \
  tools/riscv/tests/test_megrez_network_fixture.py
git commit -m "test(riscv): serve browser quality fixture"
```

### Task 2: Define and install the M8 guest contract

**Files:**

- Create: `tools/riscv/debian/rootfs/desktop_m8_browser_quality_evidence.sh`
- Create: `tools/riscv/tests/test_debian_m8_browser_quality.py`
- Modify: `tools/riscv/debian/rootfs/profiles.py`
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Modify: `Makefile`

- [ ] **Step 1: Write failing guest and builder tests**

Add `test_debian_m8_browser_quality.py` with a fake `xdotool`, `xprop`, `pgrep`,
`sha256sum`, `xwd`, `gzip`, and `curl` PATH. Freeze this ordered contract:

```python
DESKTOP_M8_FIXTURE_MARKER = (
    "DEBIAN_BROWSER_M8_FIXTURE text=cjk-latin image=png form=query"
)
DESKTOP_M8_SCROLL_MARKER = "DEBIAN_BROWSER_M8_SCROLL direction=end-home"
DESKTOP_M8_NAVIGATION_MARKER = (
    "DEBIAN_BROWSER_M8_NAVIGATION second=loaded back=loaded forward=loaded"
)
DESKTOP_M8_DOWNLOAD_MARKER = (
    "DEBIAN_BROWSER_M8_DOWNLOAD bytes=262144 "
    f"sha256={BROWSER_DOWNLOAD_SHA256}"
)
DESKTOP_M8_SOAK_MARKER = "DEBIAN_BROWSER_M8_SOAK seconds=120 process=alive"
DESKTOP_M8_CAPTURE_PREFIX = "DEBIAN_BROWSER_M8_CAPTURE bytes="
DESKTOP_M8_READY_MARKER = "DEBIAN_BROWSER_M8_READY quality=lightweight"
DESKTOP_M8_FIXED_MILESTONES = (
    DESKTOP_M8_FIXTURE_MARKER.encode(),
    DESKTOP_M8_SCROLL_MARKER.encode(),
    DESKTOP_M8_NAVIGATION_MARKER.encode(),
    DESKTOP_M8_DOWNLOAD_MARKER.encode(),
    DESKTOP_M8_SOAK_MARKER.encode(),
)
DESKTOP_M8_CAPTURE_PATTERN = re.compile(
    rb"DEBIAN_BROWSER_M8_CAPTURE bytes=([1-9][0-9]{0,7}) "
    rb"sha256=([0-9a-f]{64})(?:\r?\n|$)"
)
DESKTOP_M8_BROWSER_QUALITY_MILESTONES = (
    *(marker.decode() for marker in DESKTOP_M8_FIXED_MILESTONES),
    DESKTOP_M8_CAPTURE_PREFIX,
    DESKTOP_M8_READY_MARKER,
)
```

Run the script with `ASTERINAS_BROWSER_M8_TIMEOUT_SECONDS=10`, zero poll/settle
delays, and a loopback fixture URL. Assert exact address-bar commands, End/Home,
Alt+Left/Alt+Right, download file size/hash, one `xwd -root -silent`, deterministic
`gzip -n`, one POST, ordered markers, and no extra NetSurf process/window.

Add failure subtests for ambiguous window, title timeout, stale download,
download mismatch, browser exit during soak, xwd failure, upload rejection, and
invalid environment values. Each emits exactly one
`DEBIAN_BROWSER_M8_FAIL reason=<stable-reason>` and never emits READY.

Assert `desktop-m5-network` includes `x11-apps` in requested and identity
packages. Assert the builder installs the script as
`/usr/lib/asterinas/desktop-m8-browser-quality-evidence` and creates a oneshot
unit ordered after `asterinas-desktop-m7-baidu.service`.

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_m8_browser_quality -v
```

Expected: FAIL because the M8 script and package/service wiring are absent.

- [ ] **Step 3: Implement the guest driver**

Use the established M7 single-process/single-window checks and these bounded
operations:

```bash
quality_url="${ASTERINAS_BROWSER_M8_FIXTURE_URL%/}/browser-quality/index.html"
download_url="${ASTERINAS_BROWSER_M8_FIXTURE_URL%/}/browser-quality/download.bin"
capture_url="${ASTERINAS_BROWSER_M8_FIXTURE_URL%/}/browser-quality/capture.xwd.gz"
download=/home/asterinas/Downloads/asterinas-browser-quality.bin
capture=/run/asterinas-browser-quality.xwd.gz

xdotool key ctrl+l
xdotool type --delay 0 -- "$quality_url"
xdotool key Return
wait_for_title "Asterinas Browser Quality"
xdotool key End
xdotool key Home
xdotool key Tab
xdotool type --delay 0 -- asterinas
xdotool key Return
wait_for_title "asterinas - Asterinas Browser Quality"
xdotool mousemove --window "$window_id" 80 240 click 1
wait_for_title "Second - Asterinas Browser Quality"
```

Navigate the reciprocal link, back, and forward through exact title waits.
Drive the download URL through NetSurf, accept one visible GTK save action, and
wait for one regular non-symlink file at the exact path before hashing it.
After the soak, capture and upload:

```bash
xwd -display :0 -root -silent | gzip -n >"$capture"
capture_size="$(stat -c %s -- "$capture")"
capture_sha256="$(sha256sum -- "$capture" | cut -d' ' -f1)"
curl --fail --silent --show-error --max-time 30 \
  -H "Content-Type: application/x-xwd+gzip" \
  --data-binary "@$capture" "$capture_url"
emit "DEBIAN_BROWSER_M8_CAPTURE bytes=$capture_size sha256=$capture_sha256"
```

Use one absolute deadline for every wait and remove both temporary files on
EXIT. Install `x11-apps`; Debian 13 provides `/usr/bin/xwd` for riscv64.

- [ ] **Step 4: Wire the root image and unit target**

Add the script to `configure_desktop_services` and create:

```ini
[Unit]
Description=Asterinas Debian M8 lightweight browser quality evidence
After=asterinas-desktop-m7-baidu.service

[Service]
Type=oneshot
Environment=ASTERINAS_BROWSER_M8_TIMEOUT_SECONDS=300
TimeoutStartSec=360
ExecStart=/usr/lib/asterinas/desktop-m8-browser-quality-evidence
RemainAfterExit=yes

[Install]
WantedBy=graphical.target
```

Add `tools.riscv.tests.test_debian_m8_browser_quality` to
`test_riscv_debian_rootfs_unit`.

- [ ] **Step 5: Run GREEN and commit**

```bash
make test_riscv_debian_rootfs_unit
bash -n tools/riscv/debian/rootfs/desktop_m8_browser_quality_evidence.sh
ruff check tools/riscv/tests/test_debian_m8_browser_quality.py \
  tools/riscv/debian/rootfs/profiles.py
ruff format --check tools/riscv/tests/test_debian_m8_browser_quality.py \
  tools/riscv/debian/rootfs/profiles.py
git diff --check
git add Makefile tools/riscv/debian/rootfs/build_rootfs.sh \
  tools/riscv/debian/rootfs/profiles.py \
  tools/riscv/debian/rootfs/desktop_m8_browser_quality_evidence.sh \
  tools/riscv/tests/test_debian_m8_browser_quality.py
git commit -m "build(riscv): add lightweight browser quality guest"
```

### Task 3: Add the M8 QEMU gate

**Files:**

- Create: `tools/riscv/debian/rootfs/desktop_m8_browser_quality_gate.py`
- Modify: `tools/riscv/tests/test_debian_m8_browser_quality.py`
- Modify: `Makefile`

- [ ] **Step 1: Write failing classifier and lifecycle tests**

Require all M7 evidence followed by each M8 marker exactly once and in order.
Reject duplicate, missing, reordered, guest-failure, fatal, and capture hash
mismatches. Freeze the public classifier:

```python
def classify_desktop_m8_browser_quality(
    transcript: bytes,
    *,
    expected_debian_release: str,
) -> GateResult:
    base = classify_desktop_m7_baidu(
        transcript,
        expected_debian_release=expected_debian_release,
    )
    if not base.passed:
        return base
    if DESKTOP_M8_FAILURE_MARKER.lower() in transcript.lower():
        return GateResult(False, "browser quality guest failure", None)
    markers = (*DESKTOP_M8_FIXED_MILESTONES, DESKTOP_M8_READY_MARKER.encode())
    if any(transcript.count(marker) != 1 for marker in markers):
        return GateResult(False, "missing or duplicate browser quality evidence", None)
    captures = tuple(DESKTOP_M8_CAPTURE_PATTERN.finditer(transcript))
    if len(captures) != 1:
        return GateResult(False, "missing or duplicate browser capture", None)
    positions = (
        *(transcript.find(marker) for marker in DESKTOP_M8_FIXED_MILESTONES),
        captures[0].start(),
        transcript.find(DESKTOP_M8_READY_MARKER.encode()),
    )
    if positions != tuple(sorted(positions)) or len(set(positions)) != len(positions):
        return GateResult(False, "browser quality milestones out of order", None)
    return GateResult(True, "pass", None)
```

For the operations adapter, assert four captures named:

```python
QUALITY_CAPTURE_NAMES = (
    "desktop-m8-fixture.ppm",
    "desktop-m8-navigation.ppm",
    "desktop-m8-download.ppm",
    "desktop-m8-final.ppm",
)
```

The first three follow their matching marker; the final capture follows READY.
Any guest failure captures `desktop-m8-failure.ppm`. Publication writes result
last and includes every screenshot's standard M3 metadata.

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_m8_browser_quality -v
```

Expected: FAIL with missing `desktop_m8_browser_quality_gate`.

- [ ] **Step 3: Implement the thin M7-derived adapter**

Subclass `DesktopM7BaiduOperations`, call `super().run_protocol`, then wait for
M8 markers and call `capture_rendered_ppm` at the four defined boundaries.
Keep screenshot bytes private until publication. Use the existing
`orchestrate_systemd_m2_gate` lifecycle and `parse_gate_args`; do not add a new
QEMU launcher or process cleanup path.

The classifier first calls `classify_desktop_m7_baidu`, then validates M8 and
one syntactically strict capture marker. The operations adapter obtains the
exact bytes and identity from its inherited `FixtureServer` and compares them
with the marker before allowing a passing result:

```python
def _capture_marker(summary: Mapping[str, object]) -> bytes:
    return (
        f"DEBIAN_BROWSER_M8_CAPTURE bytes={summary['bytes']} "
        f"sha256={summary['sha256']}"
    ).encode()

summary = self.fixture.capture_summary()
payload = self.fixture.capture_payload()
if summary is None or payload is None:
    result["passed"] = False
    result["reason"] = "browser capture missing"
elif (
    summary["bytes"] != len(payload)
    or summary["sha256"] != hashlib.sha256(payload).hexdigest()
    or _capture_marker(summary) not in transcript
):
    result["passed"] = False
    result["reason"] = "browser capture evidence mismatch"
```

- [ ] **Step 4: Add the full target**

Add `test_riscv_debian_desktop_m8_browser_quality_gate` with the same eight
required artifact arguments as M7, output variable
`DEBIAN_DESKTOP_M8_BROWSER_QUALITY_GATE_OUTPUT`, `--smp 4`, and the existing
desktop boot timeout.

- [ ] **Step 5: Run GREEN and commit**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_m8_browser_quality -v
python3 -m py_compile \
  tools/riscv/debian/rootfs/desktop_m8_browser_quality_gate.py \
  tools/riscv/tests/test_debian_m8_browser_quality.py
ruff check tools/riscv/debian/rootfs/desktop_m8_browser_quality_gate.py \
  tools/riscv/tests/test_debian_m8_browser_quality.py
ruff format --check tools/riscv/debian/rootfs/desktop_m8_browser_quality_gate.py \
  tools/riscv/tests/test_debian_m8_browser_quality.py
git diff --check
git add Makefile tools/riscv/debian/rootfs/desktop_m8_browser_quality_gate.py \
  tools/riscv/tests/test_debian_m8_browser_quality.py
git commit -m "test(riscv): gate lightweight browser quality"
```

### Task 4: Bind M8 simulation to a schema-3 Megrez plan

**Files:**

- Modify: `tools/riscv/megrez_debug_contract.py`
- Modify: `tools/riscv/megrez_debug.py`
- Modify: `tools/riscv/megrez_debug_desktop.py`
- Modify: `tools/riscv/megrez_debug_board.py`
- Modify: `tools/riscv/tests/test_megrez_debug.py`
- Modify: `tools/riscv/tests/test_megrez_debug_desktop.py`

- [ ] **Step 1: Write failing schema-3 tests**

Define profile `debian-browser-quality`, schema version 3, the existing ten
artifact identities, generic Sv39, SMP=4, reboot-after=600, all existing M5/M4/
M6/M7 markers, followed by the M8 marker tuple. Assert canonical round trip,
duplicate-key rejection, old schema-2 byte compatibility, and rejection of
missing/reordered M8 markers.

Assert that `simulate_desktop` invokes
`tools.riscv.debian.rootfs.desktop_m8_browser_quality_gate`, validates all M8
screenshot metadata, reads all M8 evidence with the existing 8-MiB cap, and
publishes a plan-bound desktop pass only after the M8 classifier passes.

Assert that schema 3 accepts the exact pointer-missing degraded branch but
still records overall physical input degradation. Schema 1 remains unchanged.

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_debug \
  tools.riscv.tests.test_megrez_debug_desktop -v
```

Expected: FAIL because schema 3, the profile, and M8 simulation evidence are
not accepted.

- [ ] **Step 3: Implement the schema and simulation switch**

Add exact identities without changing schema 2:

```python
DEBIAN_BROWSER_QUALITY_PROFILE = "debian-browser-quality"
DEBIAN_BROWSER_QUALITY_SCHEMA_VERSION = 3
DEBIAN_BROWSER_QUALITY_MARKERS = (
    *DEBIAN_BROWSER_MARKERS,
    *DESKTOP_M8_BROWSER_QUALITY_MILESTONES,
)
```

Select M8 only for the schema-3 profile in `_desktop_command` and
`_validate_native_result`. Add all M8 screenshot names to the immutable
evidence tuple. Permit pointer degradation when
`plan.profile in {"debian-browser", "debian-browser-quality"}` rather than by
schema number alone.

- [ ] **Step 4: Run GREEN and commit**

```bash
make test_riscv_megrez_debug_unit
python3 -m py_compile tools/riscv/megrez_debug_contract.py \
  tools/riscv/megrez_debug.py tools/riscv/megrez_debug_desktop.py \
  tools/riscv/megrez_debug_board.py
ruff check tools/riscv/megrez_debug_contract.py tools/riscv/megrez_debug.py \
  tools/riscv/megrez_debug_desktop.py tools/riscv/megrez_debug_board.py \
  tools/riscv/tests/test_megrez_debug.py \
  tools/riscv/tests/test_megrez_debug_desktop.py
ruff format --check tools/riscv/megrez_debug_contract.py \
  tools/riscv/megrez_debug.py tools/riscv/megrez_debug_desktop.py \
  tools/riscv/megrez_debug_board.py tools/riscv/tests/test_megrez_debug.py \
  tools/riscv/tests/test_megrez_debug_desktop.py
git diff --check
git add tools/riscv/megrez_debug_contract.py tools/riscv/megrez_debug.py \
  tools/riscv/megrez_debug_desktop.py tools/riscv/megrez_debug_board.py \
  tools/riscv/tests/test_megrez_debug.py \
  tools/riscv/tests/test_megrez_debug_desktop.py
git commit -m "test(riscv): bind Megrez browser quality plan"
```

### Task 5: Collect physical capture evidence safely

**Files:**

- Modify: `tools/riscv/megrez_network_fixture.py`
- Modify: `tools/riscv/tests/test_megrez_network_fixture.py`
- Modify: `tools/riscv/megrez_debug_board.py`
- Modify: `tools/riscv/tests/test_megrez_debug.py`

- [ ] **Step 1: Write failing physical publication tests**

Run the board state machine with a complete schema-3 transcript and an
injected `FixtureServer` whose capture bytes/hash match the M8 marker, followed
by fresh U-Boot recovery. Assert
the physical evidence tuple contains `desktop-m8-capture.xwd.gz` and
`capture.json`, and result publication remains last.

Reject missing capture, unsafe capture output, hash/size mismatch, second
upload, partial transcript, missing recovery, and a capture produced by any
peer other than the planned Megrez address. Verify every failure leaves
`passed:false` and no stale passing result.

Add CLI tests requiring schema 3 to supply canonical
`--fixture-bind-address`, `--fixture-allow-peer`, and optional
`--fixture-port` (default 17894). Reject these options for schemas 1 and 2.
Require the bind address/port to match `ASTERINAS_DESKTOP_FIXTURE_URL` in the
plan bootargs and the allowed peer to match the address in
`asterinas.net=eic7700-rj45,<address>/<prefix>,<gateway>` before serial access.

- [ ] **Step 2: Run RED**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_network_fixture \
  tools.riscv.tests.test_megrez_debug.MegrezDebugBoardStateTests \
  tools.riscv.tests.test_megrez_debug.MegrezDebugRealBoardOperationsTests -v
```

Expected: FAIL because capture evidence is not yet bound to board publication.

- [ ] **Step 3: Bind the fixture to physical result publication**

Let the schema-3 board command own one `FixtureServer` bound to the configured
host RJ45 address and restricted to the planned board address. Inject it into
`RealBoardOperations`; schema 1 and schema 2 construct no fixture and retain
their current behavior. Validate before publication:

```python
summary = fixture.capture_summary()
payload = fixture.capture_payload()
if summary is None or payload is None:
    raise BoardRunFailure("browser-capture-missing")
if summary["peer"] != planned_board_address:
    raise BoardRunFailure("browser-capture-peer-mismatch")
if summary["bytes"] != len(payload):
    raise BoardRunFailure("browser-capture-size-mismatch")
if summary["sha256"] != hashlib.sha256(payload).hexdigest():
    raise BoardRunFailure("browser-capture-hash-mismatch")
```

Compare the same summary with the exact M8 serial marker. Atomically publish
the bounded in-memory payload and canonical summary before `result.json`.
Always stop and join the fixture in `finish()`, including preboot, signal, and
publication failures. Keep existing schema-1 and schema-2 evidence unchanged.

- [ ] **Step 4: Run GREEN and commit**

```bash
make test_riscv_megrez_gmac_unit
make test_riscv_megrez_debug_unit
python3 -m py_compile tools/riscv/megrez_network_fixture.py \
  tools/riscv/megrez_debug_board.py
ruff check tools/riscv/megrez_network_fixture.py \
  tools/riscv/megrez_debug_board.py \
  tools/riscv/tests/test_megrez_network_fixture.py \
  tools/riscv/tests/test_megrez_debug.py
ruff format --check tools/riscv/megrez_network_fixture.py \
  tools/riscv/megrez_debug_board.py \
  tools/riscv/tests/test_megrez_network_fixture.py \
  tools/riscv/tests/test_megrez_debug.py
git diff --check
git add tools/riscv/megrez_network_fixture.py tools/riscv/megrez_debug_board.py \
  tools/riscv/tests/test_megrez_network_fixture.py \
  tools/riscv/tests/test_megrez_debug.py
git commit -m "test(riscv): bind physical browser capture"
```

### Task 6: Build once and run the full QEMU gate

**Files:**

- Modify generated artifacts only under: `target/debian-riscv/browser-quality/`
- No source commit in this task.

- [ ] **Step 1: Run all cheap gates**

```bash
make test_riscv_debian_rootfs_unit
make test_riscv_megrez_gmac_unit
make test_riscv_megrez_debug_unit
```

Expected: all tests pass before any image build.

- [ ] **Step 2: Build one signed desktop root image**

Use the pinned Asterinas container, existing proxy, signed Debian mirror, and
content-addressed package cache. Build `desktop-m5-network` once after adding
`x11-apps`; do not rebuild for guest-script-only changes. Verify the frozen
manifest, package lock, package checksums, ext2 label/UUID, and `/usr/bin/xwd`.

- [ ] **Step 3: Run one M8 QEMU gate**

Start the fixture on the QEMU host gateway and run:

```bash
make test_riscv_debian_desktop_m8_browser_quality_gate \
  DEBIAN_KERNEL="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  DEBIAN_UBOOT="$PWD/target/qemu-uboot/current/u-boot" \
  DEBIAN_DTB="$PWD/target/qemu-uboot/current/qemu-virt.dtb" \
  DEBIAN_STAGE1_INITRAMFS="$PWD/target/debian-riscv/stage1/initramfs.cpio" \
  DEBIAN_ROOT_IMAGE="$PWD/target/debian-riscv/browser-quality/rootfs/debian-root.ext2" \
  DEBIAN_ROOT_MANIFEST="$PWD/target/debian-riscv/browser-quality/rootfs/rootfs-manifest.json" \
  DEBIAN_PACKAGES_LOCK="$PWD/target/debian-riscv/browser-quality/rootfs/packages.lock" \
  DEBIAN_PACKAGE_CHECKSUMS="$PWD/target/debian-riscv/browser-quality/rootfs/source-metadata/package-checksums" \
  DEBIAN_DESKTOP_M8_BROWSER_QUALITY_GATE_OUTPUT="$PWD/target/debian-riscv/browser-quality/qemu"
```

Expected: pass within eight minutes with all M5-M8 markers, five M8 screenshots
(four success states plus no failure screenshot), exact download identity, and
no fatal marker.

- [ ] **Step 4: Inspect every screenshot**

Open the fixture, navigation, download, final, Baidu home, and Baidu search
screenshots. Require readable Chinese/Latin text, a rendered image, visible
NetSurf chrome, expected page title, no challenge page in a passing result, and
no blank/solid-color frame. Record screenshot hashes in the QEMU result.

### Task 7: Perform one guarded Megrez acceptance run

**Files:**

- Write generated evidence under:
  `target/megrez-debug/browser-quality-<commit>/`
- No source commit until the evidence audit identifies a reproducible defect.

- [ ] **Step 1: Freeze the schema-3 plan and preflight**

Bind the exact QEMU-passing kernel, stage1, QEMU/Megrez DTBs, U-Boot, root image,
manifest, lock, checksums, bootargs, and M5-M8 markers. Require the QEMU result,
recovery result, exclusive serial ownership, current artifact identities,
fixture health, capture receiver health, and a fresh U-Boot prompt.

- [ ] **Step 2: Run exactly one board boot**

Use `asterinas.reboot_after=600`, `--timeout 900`, no hardware watchdog, the
static RJ45 profile, and the allowed board peer `10.100.19.200`. Do not transfer
the 1-GiB root image over serial and do not run Linux as the target kernel.

Expected ordered evidence: selected GMAC, M5 network/stress/HTTPS, M4 input/Xorg
classification, M6 remote/JavaScript, M7 Baidu home/search, every M8 usability
marker, exact capture upload, and fresh U-Boot recovery.

- [ ] **Step 3: Audit before claiming pass**

Replay the serial transcript through the production marker tracker in small
chunks. Verify the capture hash/size/peer against its marker, inspect the
physical screenshot, confirm no panic/Oops/fatal marker, and confirm automatic
recovery. A full interactive pass requires both keyboard and pointer evidence;
otherwise publish the exact input-degraded result without repeating the boot.

### Task 8: Document the final operator flow

**Files:**

- Modify: `tools/riscv/debian/rootfs/README.md`
- Modify: `tools/riscv/README.md`

- [ ] **Step 1: Update commands and result semantics**

Document the M8 QEMU target, fixture endpoints, screenshot upload cap, package
identity, schema-3 plan, 900-second physical timeout, one-boot policy, and the
separate browser-content/input classifications. Link the Debian `x11-apps`
package page as the provenance for RISC-V `xwd`.

- [ ] **Step 2: Run final verification**

```bash
make test_riscv_debian_rootfs_unit
make test_riscv_megrez_gmac_unit
make test_riscv_megrez_debug_unit
python3 -m py_compile \
  tools/riscv/megrez_network_fixture.py \
  tools/riscv/debian/rootfs/desktop_m8_browser_quality_gate.py \
  tools/riscv/tests/test_debian_m8_browser_quality.py
bash -n tools/riscv/debian/rootfs/desktop_m8_browser_quality_evidence.sh
ruff check tools/riscv/megrez_network_fixture.py \
  tools/riscv/debian/rootfs/desktop_m8_browser_quality_gate.py \
  tools/riscv/tests/test_debian_m8_browser_quality.py
ruff format --check tools/riscv/megrez_network_fixture.py \
  tools/riscv/debian/rootfs/desktop_m8_browser_quality_gate.py \
  tools/riscv/tests/test_debian_m8_browser_quality.py
git diff --check
```

Expected: every command exits zero and the worktree contains only the two
documentation changes.

- [ ] **Step 3: Commit**

```bash
git add tools/riscv/debian/rootfs/README.md tools/riscv/README.md
git commit -m "docs(riscv): document browser quality gate"
```
