# Megrez Boot Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an unattended, three-cycle physical boot gate that proves an immutable MMC deployment reaches the graphical Firefox service and returns to a fresh U-Boot epoch without requiring network, keyboard, mouse, HDMI capture, or partition-2 writes.

**Architecture:** Keep deployment and acceptance separate. The existing schema-2 `DebugPlan` remains the deployment manifest, and the existing `RealPhysicalGraphicsOperations` remains the implementation of U-Boot/MMC loading and recovery; a focused boot-stability subclass adds an input-independent framebuffer/Xorg/Openbox/Firefox readiness probe, bounded diagnostics, and immediate controlled reboot. The orchestrator runs three fresh instances and atomically publishes one result. Versioned MMC files are installed through RockOS only when the manifest identity changes; the gate itself is read-only with respect to the MMC and never rebuilds or retransfers artifacts.

**Tech Stack:** Python 3, `unittest`, Asterinas serial/debug-console protocols, U-Boot `ext4load`/`crc32`, systemd, persistent project Docker container.

**Executed outcome (2026-09-10):** The host-side suite passes 82 tests. The
physical result at
`target/current-main-physical-graphics/physical/boot-stability-runtime-home-03`
passes all three immutable-MMC boot/recovery cycles. The implementation also
moves mutable graphics state to tmpfs, makes the serial shell protocol
acknowledged and retryable, and lets the routine gate run on the host without
reopening container-local build paths.

---

### Task 1: Define the pure three-cycle lifecycle

**Files:**
- Create: `tools/riscv/megrez_boot_stability.py`
- Create: `tools/riscv/tests/test_megrez_boot_stability.py`

- [ ] **Step 1: Write failing result and lifecycle tests**

Define a fake cycle implementation and assert the desired API:

```python
class BootStabilityLifecycleTests(unittest.TestCase):
    def test_pass_requires_three_ready_recovered_cycles(self) -> None:
        operations = FakeOperationsFactory()
        result = run_boot_stability(
            plan(), BootStabilityConfig(), operations, clock=FakeClock()
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.completed_cycles, 3)
        self.assertTrue(all(cycle.recovered for cycle in result.cycles))
        self.assertEqual(operations.events.count("collect-diagnostics"), 3)
        self.assertEqual(operations.events.count("request-reboot"), 3)

    def test_failed_readiness_still_recovers_and_stops_new_cycles(self) -> None:
        operations = FakeOperationsFactory(fail_readiness=2)
        result = run_boot_stability(
            plan(), BootStabilityConfig(), operations, clock=FakeClock()
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.completed_cycles, 1)
        self.assertIn("recovery-2", operations.events)
        self.assertNotIn("open-3", operations.events)

    def test_fatal_diagnostics_cannot_publish_pass(self) -> None:
        operations = FakeOperationsFactory(
            diagnostics={2: b"Kernel panic - not syncing"}
        )
        result = run_boot_stability(
            plan(), BootStabilityConfig(), operations, clock=FakeClock()
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "cycle-2-fatal-diagnostics")
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_megrez_boot_stability -v
```

Expected: FAIL because `tools.riscv.megrez_boot_stability` does not exist.

- [ ] **Step 3: Implement validated result types and the lifecycle**

Add immutable evidence types and a fail-closed runner:

```python
@dataclass(frozen=True)
class BootCycleEvidence:
    cycle: int
    readiness: BootReadinessEvidence
    transport: tuple[str, ...]
    serial_sha256: str
    diagnostics_sha256: str
    boot_seconds: float
    readiness_seconds: float
    diagnostics_seconds: float
    recovery_seconds: float
    recovered: bool

@dataclass(frozen=True)
class BootStabilityResult:
    schema_version: int
    passed: bool
    physical: bool
    reason: str
    plan_sha256: str
    bootargs_sha256: str
    requested_cycles: int
    completed_cycles: int
    cycles: tuple[BootCycleEvidence, ...]

def run_boot_stability(plan, config, operations_factory, *, clock=time.monotonic):
    plan.validate()
    _validate_current_artifacts(plan)
    bootargs = physical_bootargs(plan)
    evidence = []
    for cycle_number in range(1, BOOT_STABILITY_CYCLES + 1):
        operations = operations_factory(cycle_number)
        try:
            operations.open(config.open_timeout)
            transport = operations.ensure_artifacts(plan, config.artifact_timeout)
            operations.boot(plan, bootargs, config.boot_timeout)
            readiness = operations.prove_boot_readiness(config.readiness_timeout)
            diagnostics = operations.collect_diagnostics(config.diagnostics_timeout)
            if contains_fatal_diagnostics(diagnostics):
                raise FatalDiagnosticsError(cycle_number)
            operations.request_reboot(config.reboot_timeout)
            operations.await_recovery(config.recovery_timeout)
            evidence.append(operations.evidence(...))
        finally:
            operations.close()
    return BootStabilityResult(...)
```

The actual implementation must publish failure only after attempting recovery for every started guest, stop after the first failed cycle, and keep all timing values finite, non-negative, and rounded to milliseconds.

- [ ] **Step 4: Run the lifecycle tests and verify GREEN**

Run the Step 2 command.

Expected: all lifecycle tests PASS.

### Task 2: Add bounded guest diagnostics and immediate recovery

**Files:**
- Modify: `tools/riscv/megrez_boot_stability.py`
- Modify: `tools/riscv/tests/test_megrez_boot_stability.py`

- [ ] **Step 1: Write failing protocol tests**

Require nonce-delimited diagnostics, a 256-KiB decoded limit, and an acknowledged reboot command:

```python
def test_collect_diagnostics_returns_only_nonce_delimited_payload(self) -> None:
    operations, serial = real_cycle_with_serial()
    serial.transcript = diagnostic_transcript(
        nonce="0011223344556677", payload=b"kernel log\nfailed units\n"
    )
    with mock.patch.object(secrets, "token_hex", return_value="0011223344556677"):
        payload = operations.collect_diagnostics(30)
    self.assertEqual(payload, b"kernel log\nfailed units\n")

def test_collect_diagnostics_rejects_oversized_or_mismatched_payload(self) -> None:
    operations, serial = real_cycle_with_serial()
    for transcript in (oversized_diagnostics(), mismatched_digest_diagnostics()):
        serial.transcript = transcript
        with self.subTest(transcript=transcript), self.assertRaises(HostGateError):
            operations.collect_diagnostics(30)

def test_request_reboot_requires_ack_before_waiting_for_uboot(self) -> None:
    operations, serial = real_cycle_with_serial()
    serial.transcript = b"__ASTERINAS_BOOT_REBOOT__ nonce=0011223344556677\n"
    with mock.patch.object(secrets, "token_hex", return_value="0011223344556677"):
        operations.request_reboot(10)
    self.assertIn(b"sync;", serial.send.call_args.args[0])
    self.assertIn(b"reboot -f", serial.send.call_args.args[0])
```

- [ ] **Step 2: Run the protocol tests and verify RED**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_boot_stability.BootStabilityProtocolTests -v
```

Expected: FAIL because the real cycle adapter and protocol methods are missing.

- [ ] **Step 3: Implement the real cycle adapter**

Subclass the established physical adapter only to add boot-soak operations. The
new readiness type deliberately contains no keyboard, mouse, evdev, or HDMI
fields:

```python
class RealBootCycleOperations(RealPhysicalGraphicsOperations):
    def prove_boot_readiness(self, timeout: float) -> BootReadinessEvidence:
        serial = self._require_serial()
        deadline = self._guest_phase_deadline(timeout)
        serial.wait_for(DEBUG_CONSOLE_READY.encode(), deadline)
        validate_debug_console_readiness(serial.transcript.decode("utf-8"))
        self._quiesce_external_services(deadline)
        return self._probe_boot_readiness(deadline)

    def collect_diagnostics(self, timeout: float) -> bytes:
        nonce = secrets.token_hex(8)
        command = boot_diagnostics_command(nonce)
        return self._receive_bounded_payload(
            command,
            begin=f"__ASTERINAS_BOOT_DIAGNOSTICS_BEGIN__ nonce={nonce}",
            end=f"__ASTERINAS_BOOT_DIAGNOSTICS_END__ nonce={nonce}",
            timeout=timeout,
        )

    def request_reboot(self, timeout: float) -> None:
        nonce = secrets.token_hex(8)
        marker = f"__ASTERINAS_BOOT_REBOOT__ nonce={nonce}"
        serial = self._require_serial()
        deadline = self._guest_phase_deadline(timeout)
        cursor = serial.checkpoint()
        serial.send((f"sync; printf '{marker}\\n'; reboot -f\n").encode(), deadline)
        self._wait_for_exact_line(serial, cursor, marker, deadline)
```

`boot_diagnostics_command` must collect `/proc/cmdline`, `/proc/uptime`, `/proc/mounts`, `dmesg`, `systemctl --failed`, and the Firefox/Xorg unit state. It encodes the payload with base64, publishes its byte length and SHA-256, and never reads more than 256 KiB on the host.

- [ ] **Step 4: Run the protocol and lifecycle tests and verify GREEN**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_megrez_boot_stability -v
```

Expected: all tests PASS.

### Task 3: Publish an immutable deployment result and expose the CLI

**Files:**
- Modify: `tools/riscv/megrez_boot_stability.py`
- Modify: `tools/riscv/tests/test_megrez_boot_stability.py`
- Modify: `Makefile`

- [ ] **Step 1: Write failing CLI and publication tests**

Require a complete MMC mapping, reject transfer mode, pin output below the existing physical-evidence root, and publish per-cycle logs before `result.json`:

```python
def test_cli_requires_all_three_mmc_artifacts(self) -> None:
    with self.assertRaises(SystemExit):
        parse_args(BASE_ARGS + ("--mmc-kernel", "kernel.Image"))

def test_publication_contains_result_logs_diagnostics_and_hashes(self) -> None:
    publisher.publish(result, cycle_records)
    self.assertEqual(
        set(path.name for path in output.iterdir()),
        {
            "result.json", "sha256sums.txt", "deployment.json",
            "cycle-1.serial.log", "cycle-1.diagnostics.log",
            "cycle-2.serial.log", "cycle-2.diagnostics.log",
            "cycle-3.serial.log", "cycle-3.diagnostics.log",
        },
    )
```

- [ ] **Step 2: Run the CLI/publication tests and verify RED**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_boot_stability.BootStabilityCliTests \
  tools.riscv.tests.test_megrez_boot_stability.BootStabilityPublicationTests -v
```

Expected: FAIL because the CLI and publisher are missing.

- [ ] **Step 3: Implement the MMC-only CLI and atomic publisher**

The CLI accepts exactly the existing plan, stable serial device, output directory, and three safe MMC names:

```python
parser.add_argument("device")
parser.add_argument("--plan", required=True, type=Path)
parser.add_argument("--output-directory", required=True, type=Path)
parser.add_argument("--mmc-kernel", required=True, type=safe_artifact_name)
parser.add_argument("--mmc-initramfs", required=True, type=safe_artifact_name)
parser.add_argument("--mmc-dtb", required=True, type=safe_artifact_name)
```

`deployment.json` records only immutable identity: plan SHA-256, bootargs SHA-256, three MMC names, and the plan-provided size/SHA-256/CRC32 for each artifact. Use `PinnedOutputDirectory.atomic_write`; write logs, diagnostics, deployment metadata, and `sha256sums.txt` before atomically replacing `result.json` last.

Add the unit target:

```make
.PHONY: test_riscv_megrez_boot_stability_unit
test_riscv_megrez_boot_stability_unit:
	python3 -m unittest tools.riscv.tests.test_megrez_boot_stability -v
```

- [ ] **Step 4: Run all boot-stability tests and verify GREEN**

Run:

```bash
make test_riscv_megrez_boot_stability_unit
```

Expected: all tests PASS without network or downloads.

### Task 4: Document the deployment and unattended acceptance workflow

**Files:**
- Modify: `tools/riscv/README.md`
- Modify: `tools/riscv/tests/test_megrez_boot_stability.py`

- [ ] **Step 1: Write a failing documentation contract test**

Require the operator guide to state the deployment invariants and exact command:

```python
def test_readme_documents_immutable_mmc_boot_stability_gate(self) -> None:
    text = README.read_text()
    for required in (
        "## Megrez unattended boot stability",
        "RockOS",
        "partition 2 is never written",
        "test_riscv_megrez_boot_stability_unit",
        "tools.riscv.megrez_boot_stability",
        "cycle-1.diagnostics.log",
    ):
        self.assertIn(required, text)
```

- [ ] **Step 2: Run the documentation test and verify RED**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_megrez_boot_stability.BootStabilityDocumentationTests -v
```

Expected: FAIL because the section is missing.

- [ ] **Step 3: Document changed-only deployment and rollback**

Explain this workflow in `tools/riscv/README.md`:

1. Build only in `tools/docker/run_dev_container.sh`; never delete the named container or caches for routine rebuilds.
2. Treat the canonical `DebugPlan` as the release manifest and use versioned kernel/initramfs filenames.
3. Boot RockOS only when a plan identity is not already present on MMC partition 1; upload changed files, verify hashes, retain the previous versioned files, and do not modify partition 2.
4. Run the MMC-only three-cycle gate. A deployment is accepted only when all artifacts match, all three cycles reach Firefox/Xorg readiness, diagnostics contain no fatal marker, and all three cycles recover to fresh U-Boot epochs.
5. Roll back by selecting the prior plan and its retained versioned filenames; do not rebuild the root filesystem merely to change the kernel.

- [ ] **Step 4: Run the focused suite and syntax checks**

Run:

```bash
make test_riscv_megrez_boot_stability_unit
python3 -m py_compile \
  tools/riscv/megrez_boot_stability.py \
  tools/riscv/tests/test_megrez_boot_stability.py
```

Expected: all tests and compilation checks PASS.

### Task 5: Run the physical baseline and select kernel work from evidence

**Files:**
- Produce: `target/current-main-physical-graphics/physical/boot-stability-current/result.json`
- Produce: `target/current-main-physical-graphics/physical/boot-stability-current/cycle-*.serial.log`
- Produce: `target/current-main-physical-graphics/physical/boot-stability-current/cycle-*.diagnostics.log`

- [ ] **Step 1: Confirm the board is at a fresh U-Boot prompt and run the documented MMC-only command**

Use the frozen current-main plan and already staged names:

```bash
python3 -m tools.riscv.megrez_boot_stability \
  /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0 \
  --plan target/current-main-physical-graphics/physical/plan-isolated-resolved.json \
  --output-directory target/current-main-physical-graphics/physical/boot-stability-current \
  --mmc-kernel asterinas-790ab694-34bc1cc0.Image \
  --mmc-initramfs asterinas-78c4a36c-f4d9b349-stage1.cpio \
  --mmc-dtb dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb
```

Expected: three cycles complete unattended, `passed:true`, `completed_cycles:3`, and the board is left at a fresh U-Boot prompt.

- [ ] **Step 2: Classify rather than guess at kernel follow-up**

Use the first failing invariant to choose exactly one next kernel experiment:

- Timer/deadline drift: evaluate the isolated RISC-V high-resolution timer or clocksource commits in QEMU before a physical comparison.
- Linux interface failure reported by systemd: add a focused syscall/procfs test before implementing compatibility.
- Filesystem error: reproduce against the read-only root plus tmpfs layout; never repair it by writing partition 2 during the gate.
- Panic/oops: reduce to the earliest serial marker and add the smallest architecture or driver test that reproduces it.
- Three clean cycles: do not change the kernel; record the baseline and move the next milestone to a longer soak.

- [ ] **Step 3: Commit the tested host-side gate**

```bash
git add docs/superpowers/plans/2026-09-09-megrez-boot-stability.md \
  tools/riscv/megrez_boot_stability.py \
  tools/riscv/tests/test_megrez_boot_stability.py \
  tools/riscv/README.md Makefile
git commit -m "Add unattended Megrez boot stability gate"
```
