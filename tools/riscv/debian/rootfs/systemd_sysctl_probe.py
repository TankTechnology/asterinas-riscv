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
