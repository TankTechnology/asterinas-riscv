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
