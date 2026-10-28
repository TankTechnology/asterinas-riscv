# E3 systemd-sysctl Debug Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the unmodified `systemd-sysctl.service` complete and reach `sysinit.target` on the r138 candidate kernel, using a bounded QEMU probe that first makes every `/proc/sys` write observable.

**Architecture:** One rootfs overlay replaces the systemd-sysctl unit with an instrumented copy and adds a shell probe. A host runner reuses the existing `rootfs_gate` backend to boot U-Boot, QEMU, and the frozen Debian rootfs. The runner classifies serial markers and writes a fail-closed receipt. A second run without the overlay validates the real unit.

**Tech Stack:** Python 3 stdlib, POSIX shell, systemd 257, QEMU RISC-V, ext2 debugfs, Docker.

---

### Task 1: Add the probe artifacts and artifact-contract tests

**Files:**
- Create: `tools/riscv/debian/rootfs/sysctl_probe.sh`
- Create: `tools/riscv/debian/rootfs/systemd-sysctl-probe.service`
- Create: `tools/riscv/debian/rootfs/systemd-sysctl-probe-overlay.json`
- Create: `tools/riscv/tests/test_systemd_sysctl_probe.py`

- [ ] **Step 1: Write the failing artifact tests**

Create `tools/riscv/tests/test_systemd_sysctl_probe.py` with the artifact portion:

```python
# SPDX-License-Identifier: MPL-2.0

import subprocess
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.dev_overlay import load_overlay_spec


ROOTFS = Path(__file__).resolve().parents[1] / "debian" / "rootfs"


class ProbeArtifactTests(unittest.TestCase):
    def test_probe_script_syntax(self):
        script = ROOTFS / "sysctl_probe.sh"
        self.assertTrue(script.is_file())
        for shell in ("sh", "bash"):
            completed = subprocess.run(
                [shell, "-n", str(script)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_probe_script_contract(self):
        text = (ROOTFS / "sysctl_probe.sh").read_text(encoding="utf-8")
        for required in (
            "SYSCTL_PROBE begin",
            "SYSCTL_PROBE end",
            "timeout 15",
            "/proc/sys/kernel/pid_max",
            "/proc/sys/kernel/core_pattern",
            "/usr/lib/systemd/systemd-sysctl",
            "asterinas_probe_missing",
            "readback_pid_max",
            "readback_core_pattern",
        ):
            self.assertIn(required, text)

    def test_instrumented_unit_contract(self):
        text = (ROOTFS / "systemd-sysctl-probe.service").read_text(
            encoding="utf-8"
        )
        for required in (
            "ConditionPathIsReadWrite=/proc/sys/net/",
            "Before=sysinit.target shutdown.target",
            "ExecStart=/usr/lib/asterinas/sysctl-probe.sh",
            "StandardOutput=journal+console",
            "StandardError=journal+console",
            "TimeoutSec=90s",
            "ImportCredential=sysctl.*",
        ):
            self.assertIn(required, text)

    def test_overlay_spec_contract(self):
        spec = load_overlay_spec(
            ROOTFS / "systemd-sysctl-probe-overlay.json",
            expected_profile="desktop-m4-firefox",
        )
        self.assertEqual(len(spec.files), 2)
        by_destination = {
            entry.destination: entry for entry in spec.files
        }
        probe = by_destination["/usr/lib/asterinas/sysctl-probe.sh"]
        self.assertTrue(probe.create)
        self.assertEqual(probe.mode, 0o755)
        unit = by_destination[
            "/usr/lib/systemd/system/systemd-sysctl.service"
        ]
        self.assertFalse(unit.create)
        self.assertEqual(unit.mode, 0o644)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_systemd_sysctl_probe -v
```

Expected: FAIL because the three artifact files do not exist.

- [ ] **Step 3: Create the probe script**

Create `tools/riscv/debian/rootfs/sysctl_probe.sh`:

```sh
#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Bounded diagnostic for the /proc/sys writes made by systemd-sysctl.
set -u

log() {
    printf 'SYSCTL_PROBE %s\n' "$*"
}

elapsed_ms() {
    printf '%s' $(( ($2 - $1) / 1000000 ))
}

read_key() {
    name=$1
    path=$2
    start=$(date +%s%N)
    value=$(timeout 15 cat "$path" 2>&1)
    rc=$?
    end=$(date +%s%N)
    log "step=$name rc=$rc ms=$(elapsed_ms "$start" "$end") value=$(printf '%s' "$value" | tr -d '\n')"
}

write_key() {
    name=$1
    path=$2
    value=$3
    start=$(date +%s%N)
    timeout 15 sh -c 'printf "%s\n" "$1" > "$2"' sh "$value" "$path"
    rc=$?
    end=$(date +%s%N)
    log "step=$name rc=$rc ms=$(elapsed_ms "$start" "$end")"
}

run_binary() {
    name=$1
    shift
    start=$(date +%s%N)
    timeout 15 /usr/lib/systemd/systemd-sysctl "$@"
    rc=$?
    end=$(date +%s%N)
    log "step=$name rc=$rc ms=$(elapsed_ms "$start" "$end")"
}

log "begin"
read_key read_pid_max /proc/sys/kernel/pid_max
read_key read_core_pattern /proc/sys/kernel/core_pattern
write_key write_pid_max /proc/sys/kernel/pid_max 4194304
read_key readback_pid_max /proc/sys/kernel/pid_max
write_key write_core_pattern /proc/sys/kernel/core_pattern core
read_key readback_core_pattern /proc/sys/kernel/core_pattern
run_binary binary_core /usr/lib/sysctl.d/10-coredump-debian.conf
run_binary binary_pid /usr/lib/sysctl.d/50-pid-max.conf
run_binary binary_all
read_key missing_control /proc/sys/kernel/asterinas_probe_missing
log "end"
exit 0
```

- [ ] **Step 4: Create the instrumented unit**

Create `tools/riscv/debian/rootfs/systemd-sysctl-probe.service`:

```ini
#  SPDX-License-Identifier: LGPL-2.1-or-later
#
#  This file is part of systemd.
#
#  systemd is free software; you can redistribute it and/or modify it
#  under the terms of the GNU Lesser General Public License as published by
#  the Free Software Foundation; either version 2.1 of the License, or
#  (at your option) any later version.

[Unit]
Description=Apply Kernel Variables
Documentation=man:systemd-sysctl.service(8) man:sysctl.d(5)
DefaultDependencies=no
Conflicts=shutdown.target
After=systemd-modules-load.service
Before=sysinit.target shutdown.target
ConditionPathIsReadWrite=/proc/sys/net/

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/lib/asterinas/sysctl-probe.sh
StandardOutput=journal+console
StandardError=journal+console
TimeoutSec=90s
ImportCredential=sysctl.*
```

- [ ] **Step 5: Create the overlay specification**

Create `tools/riscv/debian/rootfs/systemd-sysctl-probe-overlay.json`:

```json
{
  "schema_version": 1,
  "profile": "desktop-m4-firefox",
  "files": [
    {
      "source": "sysctl_probe.sh",
      "destination": "/usr/lib/asterinas/sysctl-probe.sh",
      "mode": "0755",
      "create": true
    },
    {
      "source": "systemd-sysctl-probe.service",
      "destination": "/usr/lib/systemd/system/systemd-sysctl.service",
      "mode": "0644"
    }
  ]
}
```

- [ ] **Step 6: Run the artifact tests and verify they pass**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_systemd_sysctl_probe -v
```

Expected: 4 tests PASS.

- [ ] **Step 7: Commit the artifacts**

```bash
git add tools/riscv/debian/rootfs/sysctl_probe.sh \
  tools/riscv/debian/rootfs/systemd-sysctl-probe.service \
  tools/riscv/debian/rootfs/systemd-sysctl-probe-overlay.json \
  tools/riscv/tests/test_systemd_sysctl_probe.py
git commit -m "test: add systemd sysctl probe artifacts"
```

---

### Task 2: Add the runner marker parser, classifier, and argument parser

**Files:**
- Create: `tools/riscv/debian/rootfs/systemd_sysctl_probe.py`
- Modify: `tools/riscv/tests/test_systemd_sysctl_probe.py`

- [ ] **Step 1: Add failing pure-function tests**

Append to `tools/riscv/tests/test_systemd_sysctl_probe.py` before the
`if __name__ == "__main__":` block:

```python
from tools.riscv.debian.rootfs.systemd_sysctl_probe import (
    SYSTEMD_SYSCTL_PROBE_BOOTARGS,
    classify_probe,
    parse_probe_args,
    parse_probe_markers,
)


def _passing_probe_transcript() -> bytes:
    return b"\n".join(
        (
            b"SYSCTL_PROBE begin",
            b"SYSCTL_PROBE step=read_pid_max rc=0 ms=1 value=2147483647",
            b"SYSCTL_PROBE step=read_core_pattern rc=0 ms=1 value=core",
            b"SYSCTL_PROBE step=write_pid_max rc=0 ms=1",
            b"SYSCTL_PROBE step=readback_pid_max rc=0 ms=1 value=4194304",
            b"SYSCTL_PROBE step=write_core_pattern rc=0 ms=1",
            b"SYSCTL_PROBE step=readback_core_pattern rc=0 ms=1 value=core",
            b"SYSCTL_PROBE step=binary_all rc=0 ms=2",
            b"SYSCTL_PROBE end",
            b"[  OK  ] Started systemd-sysctl.service - Apply Kernel Variables.",
            b"[  OK  ] Reached target sysinit.target - System Initialization.",
        )
    )


class ProbeRunnerTests(unittest.TestCase):
    def test_bootargs_target_sysinit_directly(self):
        self.assertIn("systemd.unit=sysinit.target", SYSTEMD_SYSCTL_PROBE_BOOTARGS)
        self.assertIn("--root-init=systemd", SYSTEMD_SYSCTL_PROBE_BOOTARGS)
        self.assertNotIn("basic.target", SYSTEMD_SYSCTL_PROBE_BOOTARGS)

    def test_parse_probe_markers(self):
        steps = parse_probe_markers(_passing_probe_transcript())
        self.assertEqual(len(steps), 7)
        self.assertEqual(steps[0]["step"], "read_pid_max")
        self.assertEqual(steps[0]["rc"], 0)
        self.assertIn("value=2147483647", steps[0]["detail"])
        self.assertEqual(steps[3]["step"], "readback_pid_max")

    def test_classify_probe_pass(self):
        passed, reason = classify_probe(_passing_probe_transcript(), "probe")
        self.assertTrue(passed, reason)

    def test_classify_service_pass(self):
        transcript = (
            b"[  OK  ] Started systemd-sysctl.service - Apply Kernel Variables.\n"
            b"[  OK  ] Reached target sysinit.target - System Initialization.\n"
        )
        passed, reason = classify_probe(transcript, "service")
        self.assertTrue(passed, reason)

    def test_classify_probe_rejects_timeout_step(self):
        transcript = _passing_probe_transcript().replace(
            b"step=write_pid_max rc=0", b"step=write_pid_max rc=124"
        )
        passed, reason = classify_probe(transcript, "probe")
        self.assertFalse(passed)
        self.assertEqual(reason, "step-timeout:write_pid_max")

    def test_classify_probe_rejects_failed_step(self):
        transcript = _passing_probe_transcript().replace(
            b"step=write_core_pattern rc=0", b"step=write_core_pattern rc=1"
        )
        passed, reason = classify_probe(transcript, "probe")
        self.assertFalse(passed)
        self.assertEqual(reason, "step-failed:write_core_pattern:1")

    def test_classify_probe_rejects_wrong_readback(self):
        transcript = _passing_probe_transcript().replace(
            b"value=4194304", b"value=2147483647"
        )
        passed, reason = classify_probe(transcript, "probe")
        self.assertFalse(passed)
        self.assertEqual(reason, "pid-max-readback")

    def test_classify_rejects_failed_unit(self):
        transcript = _passing_probe_transcript() + (
            b"\n[FAILED] Failed to start systemd-sysctl.service\n"
        )
        passed, reason = classify_probe(transcript, "probe")
        self.assertFalse(passed)
        self.assertEqual(reason, "systemd-sysctl-failed")

    def test_classify_rejects_missing_sysinit(self):
        transcript = _passing_probe_transcript().replace(
            b"[  OK  ] Reached target sysinit.target - System Initialization.", b""
        )
        passed, reason = classify_probe(transcript, "probe")
        self.assertFalse(passed)
        self.assertEqual(reason, "sysinit-not-reached")

    def test_parse_probe_args_extracts_mode(self):
        arguments = [
            "--kernel", "kernel",
            "--uboot", "uboot",
            "--dtb", "dtb",
            "--stage1-initramfs", "stage1",
            "--root-image", "root",
            "--root-manifest", "manifest",
            "--packages-lock", "lock",
            "--package-checksums", "checksums",
            "--output-directory", "output",
            "--mode", "service",
        ]
        config, mode = parse_probe_args(arguments)
        self.assertEqual(mode, "service")
        self.assertEqual(str(config.kernel), "kernel")
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_systemd_sysctl_probe -v
```

Expected: FAIL with `ModuleNotFoundError: tools.riscv.debian.rootfs.systemd_sysctl_probe`.

- [ ] **Step 3: Create the runner module with pure functions**

Create `tools/riscv/debian/rootfs/systemd_sysctl_probe.py` with the constants,
parser, classifier, and argument parser:

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bounded QEMU probe for the E3 systemd sysctl boundary."""

from __future__ import annotations

import json
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from tools.riscv.debian.rootfs.contract import load_manifest
from tools.riscv.debian.rootfs.gate_protocol import qemu_argv
from tools.riscv.debian.rootfs.gate_runtime import (
    GateTermination,
    TerminationSignalState,
)
from tools.riscv.debian.rootfs.rootfs_gate import (
    GateConfig,
    GateFailure,
    parse_gate_args,
)
from tools.riscv.debian.rootfs.rootfs_gate_backend import (
    ConcreteOperations,
    _safe_output,
)


SYSTEMD_SYSCTL_PROBE_BOOTARGS = (
    "console=ttyS0 loglevel=4 systemd.unit=sysinit.target "
    "init=/init -- --root-init=systemd"
)

PROBE_BEGIN = b"SYSCTL_PROBE begin"
PROBE_END = b"SYSCTL_PROBE end"
SYSINIT_REACHED = b"Reached target sysinit.target"
SYSCTL_STARTED = b"Started systemd-sysctl.service"
SYSCTL_FINISHED = b"Finished systemd-sysctl.service"
SYSCTL_FAILED = b"Failed to start systemd-sysctl.service"

_PROBE_LINE = re.compile(
    rb"^SYSCTL_PROBE step=([^ ]+) rc=(-?[0-9]+) ms=([0-9]+)(?: (.*))?$",
    re.MULTILINE,
)
_VALUE = re.compile(r"(?:^| )value=([^ ]+)")


def parse_probe_markers(transcript: bytes) -> list[dict[str, object]]:
    """Parse the probe's one-line step markers from a serial transcript."""

    steps: list[dict[str, object]] = []
    for match in _PROBE_LINE.finditer(transcript):
        item: dict[str, object] = {
            "step": match.group(1).decode("ascii", "replace"),
            "rc": int(match.group(2)),
            "ms": int(match.group(3)),
        }
        detail = match.group(4)
        if detail:
            item["detail"] = detail.decode("utf-8", "replace")
        steps.append(item)
    return steps


def _step_value(steps: list[dict[str, object]], name: str) -> str | None:
    for step in steps:
        if step.get("step") == name:
            detail = str(step.get("detail", ""))
            match = _VALUE.search(detail)
            if match:
                return match.group(1)
    return None


def classify_probe(transcript: bytes, mode: str) -> tuple[bool, str]:
    """Return whether the transcript meets the mode's stop condition."""

    if mode not in ("probe", "service"):
        return False, "invalid-mode"
    if SYSINIT_REACHED not in transcript:
        return False, "sysinit-not-reached"
    if SYSCTL_FAILED in transcript:
        return False, "systemd-sysctl-failed"
    if SYSCTL_STARTED not in transcript and SYSCTL_FINISHED not in transcript:
        return False, "systemd-sysctl-not-started"
    if mode == "probe":
        if PROBE_BEGIN not in transcript:
            return False, "probe-not-started"
        if PROBE_END not in transcript:
            return False, "probe-incomplete"
        steps = parse_probe_markers(transcript)
        if not steps:
            return False, "probe-no-steps"
        for step in steps:
            name = str(step["step"])
            if step["rc"] == 124:
                return False, f"step-timeout:{name}"
            if step["rc"] != 0:
                return False, f"step-failed:{name}:{step['rc']}"
        if _step_value(steps, "readback_pid_max") != "4194304":
            return False, "pid-max-readback"
        if _step_value(steps, "readback_core_pattern") != "core":
            return False, "core-pattern-readback"
    return True, "pass"


def parse_probe_args(arguments: list[str] | None) -> tuple[GateConfig, str]:
    """Reuse the canonical gate arguments and extract the probe mode."""

    values = list(arguments) if arguments is not None else sys.argv[1:]
    mode = "probe"
    if "--mode" in values:
        index = values.index("--mode")
        if index + 1 >= len(values):
            raise GateFailure("--mode requires a value")
        mode = values[index + 1]
        del values[index : index + 2]
    if mode not in ("probe", "service"):
        raise GateFailure(f"invalid mode: {mode}")
    return parse_gate_args(values), mode


def _reason(error: BaseException, fallback: str) -> str:
    candidate = getattr(error, "reason", None)
    return candidate if isinstance(candidate, str) and candidate else fallback


def _publish_state(
    operations: Any,
    config: GateConfig,
    *,
    status: str,
    phase: str,
    snapshots: Mapping[str, str],
    identity: Mapping[str, object],
    reason: str | None = None,
) -> None:
    state: dict[str, object] = {
        "schema": 1,
        "status": status,
        "phase": phase,
        "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_sha256": dict(snapshots),
        "identity": dict(identity),
    }
    if reason is not None:
        state["reason"] = reason
    publisher = getattr(operations, "publish_state", None)
    if publisher is not None:
        publisher(config, state)
```

- [ ] **Step 4: Run the tests and verify they pass**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_systemd_sysctl_probe -v
```

Expected: 13 tests PASS.

- [ ] **Step 5: Commit the pure functions**

```bash
git add tools/riscv/debian/rootfs/systemd_sysctl_probe.py \
  tools/riscv/tests/test_systemd_sysctl_probe.py
git commit -m "test: add systemd sysctl probe classifier"
```

---

### Task 3: Add the QEMU runner and orchestration

**Files:**
- Modify: `tools/riscv/debian/rootfs/systemd_sysctl_probe.py`
- Modify: `tools/riscv/tests/test_systemd_sysctl_probe.py`

- [ ] **Step 1: Add failing orchestration tests**

Append to `tools/riscv/tests/test_systemd_sysctl_probe.py`:

```python
from tools.riscv.debian.rootfs.systemd_sysctl_probe import orchestrate_probe


class _FakeOperations:
    mode = "probe"

    def __init__(self, transcript: bytes, protocol_error: Exception | None = None):
        self.transcript = transcript
        self.protocol_error = protocol_error
        self.states = []
        self.result = None

    def invalidate(self, config):
        self.invalidated = True

    def snapshot_inputs(self, config):
        return {"kernel": "k", "root_image": "r"}

    def validate_inputs(self, config, snapshots):
        return {"debian_release": "13.7", "profile": "desktop-m4-firefox"}

    def prepare(self, config, snapshots, identity):
        return {"root_disk_format": "qcow2"}

    def publish_state(self, config, state):
        self.states.append(state)

    def launch(self, config, prepared):
        return {"session": True}

    def run_protocol(self, session, config):
        if self.protocol_error is not None:
            raise self.protocol_error

    def request_quit(self, session, config):
        pass

    def close_monitor(self, session):
        pass

    def cleanup_process(self, session, config):
        pass

    def drain_serial(self, session, config):
        return self.transcript

    def publish(self, config, prepared, transcript, result):
        self.result = result


class ProbeOrchestrationTests(unittest.TestCase):
    def test_orchestrate_probe_passes_and_publishes_steps(self):
        operations = _FakeOperations(_passing_probe_transcript())
        result = orchestrate_probe(object(), operations)
        self.assertTrue(result["passed"])
        self.assertEqual(result["reason"], "pass")
        self.assertEqual(len(result["steps"]), 7)
        self.assertEqual(result["mode"], "probe")
        self.assertEqual(operations.result, result)

    def test_orchestrate_probe_records_protocol_failure(self):
        operations = _FakeOperations(
            _passing_probe_transcript(), GateFailure("protocol-timeout")
        )
        result = orchestrate_probe(object(), operations)
        self.assertFalse(result["passed"])
        self.assertEqual(result["reason"], "protocol-timeout")
        self.assertEqual(result["steps"], [])
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_systemd_sysctl_probe -v
```

Expected: FAIL because `orchestrate_probe` is not defined.

- [ ] **Step 3: Add the orchestration function**

Append to `tools/riscv/debian/rootfs/systemd_sysctl_probe.py`:

```python
def orchestrate_probe(config: Any, operations: Any) -> dict[str, object]:
    """Run one bounded boot and publish a fail-closed result."""

    operations.invalidate(config)
    snapshots: Mapping[str, str] = {}
    identity: Mapping[str, object] = {}
    prepared: Any = None
    session: Any = None
    transcript = b""
    reason: str | None = None
    _publish_state(
        operations,
        config,
        status="running",
        phase="start",
        snapshots=snapshots,
        identity=identity,
    )

    try:
        snapshots = operations.snapshot_inputs(config)
        _publish_state(
            operations,
            config,
            status="running",
            phase="snapshot",
            snapshots=snapshots,
            identity=identity,
        )
        identity = operations.validate_inputs(config, snapshots)
        _publish_state(
            operations,
            config,
            status="running",
            phase="validate",
            snapshots=snapshots,
            identity=identity,
        )
        prepared = operations.prepare(config, snapshots, identity)
        _publish_state(
            operations,
            config,
            status="running",
            phase="prepare",
            snapshots=snapshots,
            identity=identity,
        )
        session = operations.launch(config, prepared)
        _publish_state(
            operations,
            config,
            status="running",
            phase="launch",
            snapshots=snapshots,
            identity=identity,
        )
    except BaseException as error:
        reason = _reason(error, "setup")

    if session is not None and reason is None:
        watchdog_stop: threading.Event | None = None
        watchdog: threading.Thread | None = None
        timeout = getattr(config, "boot_timeout", None)
        if isinstance(timeout, (int, float)) and timeout > 0:
            watchdog_stop = threading.Event()

            def protocol_watchdog() -> None:
                assert watchdog_stop is not None
                if watchdog_stop.wait(timeout):
                    return
                try:
                    operations.cleanup_process(session, config)
                except BaseException:
                    pass

            watchdog = threading.Thread(
                target=protocol_watchdog,
                name="systemd-sysctl-probe-watchdog",
                daemon=True,
            )
            watchdog.start()
        try:
            operations.run_protocol(session, config)
        except BaseException as error:
            reason = _reason(error, "protocol")
        finally:
            if watchdog_stop is not None and watchdog is not None:
                watchdog_stop.set()
                watchdog.join(timeout=1.0)

    if session is not None:
        try:
            operations.request_quit(session, config)
        except BaseException as error:
            if reason is None:
                reason = _reason(error, "quit")
        try:
            operations.close_monitor(session)
        except BaseException as error:
            if reason is None:
                reason = _reason(error, "close")
        try:
            operations.cleanup_process(session, config)
        except BaseException as error:
            if reason is None:
                reason = _reason(error, "cleanup")
        try:
            transcript = operations.drain_serial(session, config)
        except BaseException as error:
            if reason is None:
                reason = _reason(error, "drain")

    if reason is None:
        passed, classified_reason = classify_probe(transcript, operations.mode)
        if not passed:
            reason = classified_reason

    result: dict[str, object] = {
        "passed": reason is None,
        "reason": reason or "pass",
        "mode": operations.mode,
        "input_sha256": dict(snapshots),
        "steps": parse_probe_markers(transcript),
        "debian_release": identity.get("debian_release"),
        "profile": identity.get("profile"),
    }
    _publish_state(
        operations,
        config,
        status="complete" if result["passed"] else "failed",
        phase="finished",
        snapshots=snapshots,
        identity=identity,
        reason=str(result["reason"]) if not result["passed"] else None,
    )
    operations.publish(config, prepared, transcript, result)
    return result
```

- [ ] **Step 4: Add the QEMU backend class and CLI**

Append to `tools/riscv/debian/rootfs/systemd_sysctl_probe.py`:

```python
class SystemdSysctlProbeOperations(ConcreteOperations):
    """One-boot QEMU adapter over the persistent-root gate backend."""

    def __init__(self, config: GateConfig, *, mode: str) -> None:
        if mode not in ("probe", "service"):
            raise GateFailure(f"invalid mode: {mode}")
        super().__init__(config)
        self.mode = mode

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        arguments.setdefault("smp", 4)
        arguments.setdefault("dtb_enabled_cpu_count", 4)
        return qemu_argv(**arguments)

    def invalidate(self, config: GateConfig) -> None:
        self._require_config(config)
        self._require_output().invalidate(
            "boot.ext4",
            "debian-root.run.qcow2",
            "systemd-sysctl-probe.serial.log",
            "result.json",
        )

    def validate_inputs(
        self, config: GateConfig, snapshots: Mapping[str, str]
    ) -> Mapping[str, object]:
        identity = dict(super().validate_inputs(config, snapshots))
        manifest = load_manifest(self.input_paths["manifest"])
        if manifest.profile != "desktop-m4-firefox":
            raise GateFailure(
                "rootfs manifest is not the desktop-m4-firefox profile"
            )
        identity["profile"] = manifest.profile
        return identity

    def launch(self, config: GateConfig, prepared: Any) -> dict[str, Any]:
        return super().launch(config, prepared, 1)

    def run_protocol(self, session: Mapping[str, Any], config: GateConfig) -> None:
        deadline = time.monotonic() + config.boot_timeout
        serial = session["serial"]
        serial.wait_for(b"=> ", deadline)
        commands = (
            "virtio scan",
            "ext4load virtio 0:0 0x80200000 /asterinas.booti",
            "ext4load virtio 0:0 0x88000000 /qemu-virt.dtb",
            "fdt addr 0x88000000",
            "ext4load virtio 0:0 0x83000000 /stage1-initramfs.cpio",
            "setenv initrd_size ${filesize}",
            f'setenv bootargs "{SYSTEMD_SYSCTL_PROBE_BOOTARGS}"',
        )
        for index, command in enumerate(commands, 1):
            self._send_uboot(session, command, index, deadline)
        marker = f"__ASTERINAS_SYSCTL_PROBE_BOOT_{secrets.token_hex(8).upper()}__"
        split = len(marker) // 2
        serial.send(
            (
                f"setenv ast_ba {marker[:split]}; setenv ast_bb {marker[split:]}; "
                "echo ${ast_ba}${ast_bb}; booti 0x80200000 "
                "0x83000000:${initrd_size} 0x88000000\n"
            ).encode(),
            deadline,
        )
        serial.wait_for(marker.encode(), deadline)
        serial.wait_for(b"Starting kernel ...", deadline)
        if self.mode == "probe":
            serial.wait_for(PROBE_BEGIN, time.monotonic() + config.boot_timeout)
            serial.wait_for(PROBE_END, time.monotonic() + config.boot_timeout)
        serial.wait_for(
            SYSINIT_REACHED, time.monotonic() + config.boot_timeout
        )

    def publish(
        self,
        config: GateConfig,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None:
        self._require_config(config)
        output = self._require_output()
        result["qemu_argv"] = self._attempted_argv
        if prepared is not None:
            result["root_disk_format"] = prepared.get(
                "root_disk_format", "raw"
            )
        output.atomic_write(
            "systemd-sysctl-probe.serial.log", transcript
        )
        document = json.dumps(result, indent=2, sort_keys=True) + "\n"
        output.atomic_write("result.json", document.encode())


def main(arguments: list[str] | None = None) -> int:
    try:
        config, mode = parse_probe_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), SystemdSysctlProbeOperations(
            config, mode=mode
        ) as operations:
            result = orchestrate_probe(config, operations)
        print(
            f"systemd-sysctl-probe: passed={result['passed']} "
            f"reason={result['reason']}"
        )
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"systemd-sysctl-probe: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"systemd-sysctl-probe: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run the tests and verify they pass**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_systemd_sysctl_probe -v
python3 -m compileall -q tools/riscv/debian/rootfs/systemd_sysctl_probe.py
```

Expected: 15 tests PASS and compileall exits 0.

- [ ] **Step 6: Commit the runner**

```bash
git add tools/riscv/debian/rootfs/systemd_sysctl_probe.py \
  tools/riscv/tests/test_systemd_sysctl_probe.py
git commit -m "feat: add bounded systemd sysctl probe runner"
```

---

### Task 4: Materialize the probe rootfs and run S1

**Files:**
- Runtime output: `/mnt/mydisk/qemu-systemd-sysctl-probe-r136-rootfs`
- Runtime output: `/mnt/mydisk/qemu-systemd-sysctl-probe-r136`
- Create: `docs/porting/evidence/2026-10-20-systemd-sysctl-probe-r136.md`

- [ ] **Step 1: Materialize the instrumented rootfs**

Run from the repository root:

```bash
docker run --rm --privileged --network=host \
  -v /dev:/dev \
  -v "$PWD":/root/asterinas \
  -v /mnt/mydisk:/mnt/mydisk \
  -w /root/asterinas \
  --entrypoint python3 \
  asterinas/asterinas:0.18.0-20260702-riscv-rootfs \
  -m tools.riscv.debian.rootfs.dev_overlay materialize \
  --base-dir /mnt/mydisk/asterinas-firefox-rootfs-20261020-capab-ab-r122c \
  --spec tools/riscv/debian/rootfs/systemd-sysctl-probe-overlay.json \
  --output-dir /mnt/mydisk/qemu-systemd-sysctl-probe-r136-rootfs
```

Expected: the command prints a derived image hash and exits 0. The output
directory contains `debian-root.ext2`, `rootfs-manifest.json`,
`packages.lock`, `source-metadata/`, and `dev-overlay-manifest.json`.

- [ ] **Step 2: Verify the derived inputs**

Run:

```bash
docker run --rm \
  -v /mnt/mydisk:/mnt/mydisk:ro \
  --entrypoint sh \
  asterinas/asterinas:0.18.0-20260702-riscv-rootfs \
  -c 'cd /mnt/mydisk/qemu-systemd-sysctl-probe-r136-rootfs && sha256sum debian-root.ext2 rootfs-manifest.json && debugfs -R "cat /usr/lib/systemd/system/systemd-sysctl.service" debian-root.ext2 2>/dev/null | head -30 && debugfs -R "stat /usr/lib/asterinas/sysctl-probe.sh" debian-root.ext2 2>/dev/null | head -8'
```

Expected: the unit contains `ExecStart=/usr/lib/asterinas/sysctl-probe.sh`
and `StandardOutput=journal+console`; the probe script exists with mode 0755.

- [ ] **Step 3: Run the S1 probe**

Run:

```bash
docker run --rm --privileged --network=host \
  -v /dev:/dev \
  -v "$PWD":/root/asterinas \
  -v /mnt/mydisk:/mnt/mydisk \
  -w /root/asterinas \
  --entrypoint python3 \
  asterinas/asterinas:0.18.0-20260702-riscv-rootfs \
  -m tools.riscv.debian.rootfs.systemd_sysctl_probe \
  --kernel /mnt/mydisk/aster-kernel-pidmax-corepattern-fix-r138.Image \
  --uboot target/qemu-uboot/cache/u-boot-build/u-boot \
  --dtb target/qemu-uboot/debian-root/qemu-virt.dtb \
  --stage1-initramfs target/debian-riscv/stage1-procfix-20261017/initramfs.cpio \
  --root-image /mnt/mydisk/qemu-systemd-sysctl-probe-r136-rootfs/debian-root.ext2 \
  --root-manifest /mnt/mydisk/qemu-systemd-sysctl-probe-r136-rootfs/rootfs-manifest.json \
  --packages-lock /mnt/mydisk/qemu-systemd-sysctl-probe-r136-rootfs/packages.lock \
  --package-checksums /mnt/mydisk/qemu-systemd-sysctl-probe-r136-rootfs/source-metadata/package-checksums \
  --output-directory /mnt/mydisk/qemu-systemd-sysctl-probe-r136 \
  --root-copy-mode qcow2 \
  --boot-timeout 180 \
  --mode probe
```

Expected: the command prints `passed=True reason=pass`. If it prints
`passed=False`, do not rerun; go to Step 4 and classify the first failing step.

- [ ] **Step 4: Read the result and serial log**

Run:

```bash
docker run --rm \
  -v /mnt/mydisk:/mnt/mydisk:ro \
  --entrypoint sh \
  asterinas/asterinas:0.18.0-20260702-riscv-rootfs \
  -c 'cat /mnt/mydisk/qemu-systemd-sysctl-probe-r136/result.json; echo; grep -a "SYSCTL_PROBE\|Started systemd-sysctl\|Failed to start systemd-sysctl\|Reached target sysinit" /mnt/mydisk/qemu-systemd-sysctl-probe-r136/systemd-sysctl-probe.serial.log'
```

Expected on success: every step has `rc=0`, `readback_pid_max` has
`value=4194304`, `readback_core_pattern` has `value=core`, the unit is
`Started`, and `sysinit.target` is reached.

- [ ] **Step 5: Write the S1 evidence page**

Create `docs/porting/evidence/2026-10-20-systemd-sysctl-probe-r136.md` with:

- the gate name and `status` (`pass` only if `result.json.passed` is true);
- the input hash table copied from `result.json.input_sha256`, plus the kernel,
  U-Boot, DTB, Stage1, and derived-root hashes;
- the output directory path;
- the step table copied from `result.json.steps` (step, rc, ms, detail);
- the serial-log grep block for `SYSCTL_PROBE`, `systemd-sysctl`, and
  `sysinit.target`;
- the interpretation:
  - all rc=0, readbacks correct: the kernel fix works at syscall level;
  - any rc=124: that step is the hang point;
  - any ENOENT/EOPNOTSUPP/EINVAL: record the exact step and errno;
  - no `SYSCTL_PROBE begin`: the replacement unit or its condition did not run.
- a `Next` line: S2 if pass, otherwise the exact failing step and no rerun.

- [ ] **Step 6: Commit the S1 evidence**

```bash
git add docs/porting/evidence/2026-10-20-systemd-sysctl-probe-r136.md
git commit -m "docs: record systemd sysctl probe r136"
```

---

### Task 5: Run S2 with the unmodified unit

**Files:**
- Runtime output: `/mnt/mydisk/qemu-systemd-sysinit-r137`
- Create: `docs/porting/evidence/2026-10-20-systemd-sysinit-r137.md`

- [ ] **Step 1: Confirm S1 passed**

Do not start S2 unless the S1 evidence page says `status: pass` and every
`SYSCTL_PROBE` step has `rc=0`. If S1 failed, stop after Task 4 and report the
failing step.

- [ ] **Step 2: Run S2 against the frozen base rootfs**

Run:

```bash
docker run --rm --privileged --network=host \
  -v /dev:/dev \
  -v "$PWD":/root/asterinas \
  -v /mnt/mydisk:/mnt/mydisk \
  -w /root/asterinas \
  --entrypoint python3 \
  asterinas/asterinas:0.18.0-20260702-riscv-rootfs \
  -m tools.riscv.debian.rootfs.systemd_sysctl_probe \
  --kernel /mnt/mydisk/aster-kernel-pidmax-corepattern-fix-r138.Image \
  --uboot target/qemu-uboot/cache/u-boot-build/u-boot \
  --dtb target/qemu-uboot/debian-root/qemu-virt.dtb \
  --stage1-initramfs target/debian-riscv/stage1-procfix-20261017/initramfs.cpio \
  --root-image /mnt/mydisk/asterinas-firefox-rootfs-20261020-capab-ab-r122c/debian-root.ext2 \
  --root-manifest /mnt/mydisk/asterinas-firefox-rootfs-20261020-capab-ab-r122c/rootfs-manifest.json \
  --packages-lock /mnt/mydisk/asterinas-firefox-rootfs-20261020-capab-ab-r122c/packages.lock \
  --package-checksums /mnt/mydisk/asterinas-firefox-rootfs-20261020-capab-ab-r122c/source-metadata/package-checksums \
  --output-directory /mnt/mydisk/qemu-systemd-sysinit-r137 \
  --root-copy-mode qcow2 \
  --boot-timeout 180 \
  --mode service
```

Expected: the command prints `passed=True reason=pass`.

- [ ] **Step 3: Read the S2 result**

Run:

```bash
docker run --rm \
  -v /mnt/mydisk:/mnt/mydisk:ro \
  --entrypoint sh \
  asterinas/asterinas:0.18.0-20260702-riscv-rootfs \
  -c 'cat /mnt/mydisk/qemu-systemd-sysinit-r137/result.json; echo; grep -a "systemd-sysctl\|Reached target sysinit" /mnt/mydisk/qemu-systemd-sysinit-r137/systemd-sysctl-probe.serial.log'
```

Expected: `Started systemd-sysctl.service`, no `[FAILED]`, and
`Reached target sysinit.target`.

- [ ] **Step 4: Write the S2 evidence page**

Create `docs/porting/evidence/2026-10-20-systemd-sysinit-r137.md` with:

- the gate name and `status` (`pass` or `blocked`);
- the same input hash table as S1, noting that the root image is the frozen
  base without the probe overlay;
- the output directory path;
- the serial-log grep block;
- the conclusion: S2 pass closes the systemd-sysctl boundary for this input;
  S2 fail names the next boundary and must not be followed by another run in
  this gate.

- [ ] **Step 5: Commit the S2 evidence**

```bash
git add docs/porting/evidence/2026-10-20-systemd-sysinit-r137.md
git commit -m "docs: record systemd sysinit r137"
```

---

### Task 6: Final verification

**Files:**
- No source changes.

- [ ] **Step 1: Run the focused unit suite**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_systemd_sysctl_probe -v
```

Expected: all tests PASS.

- [ ] **Step 2: Run static checks**

Run:

```bash
python3 -m compileall -q tools/riscv/debian/rootfs tools/riscv/tests
sh -n tools/riscv/debian/rootfs/sysctl_probe.sh
bash -n tools/riscv/debian/rootfs/sysctl_probe.sh
git diff --check
```

Expected: all exit 0 with no output except the compileall/`git diff --check`
silence.

- [ ] **Step 3: Report the final state**

Summarize:

- S1 result: pass or the exact failing step and errno;
- S2 result: pass or the next boundary;
- the evidence page paths;
- explicitly state that E3 (official Firefox scene) is still not claimed by
  this gate.
