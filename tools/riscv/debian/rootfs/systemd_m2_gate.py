#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Bounded orchestration for the Debian systemd M2 two-boot gate."""

from __future__ import annotations

import json
import secrets
import sys
import time
from typing import Any, Mapping, Protocol

from tools.riscv.debian.rootfs.gate_protocol import (
    classify_systemd_m2,
    qemu_argv,
)
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
from tools.riscv.debian.rootfs.contract import load_manifest


SYSTEMD_M2_BOOTARGS = "console=ttyS0 loglevel=4 init=/init -- --root-init=systemd"


def systemd_m2_qemu_argv(**arguments: Any) -> tuple[str, ...]:
    """Construct the no-network QEMU contract while permitting SBI reboot."""

    arguments.setdefault("smp", 4)
    arguments.setdefault("dtb_enabled_cpu_count", 4)
    arguments["allow_reboot"] = True
    return qemu_argv(**arguments)


class Operations(Protocol):
    """Injected artifact and process operations used by the orchestrator."""

    def invalidate(self, config: Any) -> None: ...

    def snapshot_inputs(self, config: Any) -> Mapping[str, str]: ...

    def validate_inputs(
        self, config: Any, snapshots: Mapping[str, str]
    ) -> Mapping[str, object]: ...

    def prepare(
        self,
        config: Any,
        snapshots: Mapping[str, str],
        identity: Mapping[str, object],
    ) -> Any: ...

    def launch(self, config: Any, prepared: Any) -> Any: ...

    def run_protocol(self, session: Any, config: Any) -> None: ...

    def request_quit(self, session: Any, config: Any) -> None: ...

    def close_monitor(self, session: Any) -> None: ...

    def cleanup_process(self, session: Any, config: Any) -> None: ...

    def drain_serial(self, session: Any, config: Any) -> bytes: ...

    def hash_final_root(self, config: Any, prepared: Any) -> str: ...

    def publish(
        self,
        config: Any,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None: ...


def _reason(error: BaseException, fallback: str) -> str:
    candidate = getattr(error, "reason", None)
    return candidate if isinstance(candidate, str) and candidate else fallback


def orchestrate_systemd_m2_gate(
    config: Any, operations: Operations
) -> dict[str, object]:
    """Run one QEMU process across two boots and publish fail-closed evidence."""

    operations.invalidate(config)
    snapshots: Mapping[str, str] = {}
    identity: Mapping[str, object] = {}
    prepared: Any = None
    session: Any = None
    transcript = b""
    reason: str | None = None

    setup = (
        ("snapshot", lambda: operations.snapshot_inputs(config)),
        ("validate", lambda: operations.validate_inputs(config, snapshots)),
        ("prepare", lambda: operations.prepare(config, snapshots, identity)),
    )
    for phase, operation in setup:
        try:
            value = operation()
            if phase == "snapshot":
                snapshots = value
            elif phase == "validate":
                identity = value
            else:
                prepared = value
        except BaseException as error:
            reason = _reason(error, phase)
            break

    if reason is None:
        try:
            session = operations.launch(config, prepared)
        except BaseException as error:
            reason = _reason(error, "launch")

    if session is not None:
        if reason is None:
            try:
                operations.run_protocol(session, config)
            except BaseException as error:
                reason = _reason(error, "protocol")
        if reason is None:
            try:
                operations.request_quit(session, config)
            except BaseException as error:
                reason = _reason(error, "quit")

        teardown = (
            ("close", lambda: operations.close_monitor(session)),
            ("cleanup", lambda: operations.cleanup_process(session, config)),
            ("drain", lambda: operations.drain_serial(session, config)),
        )
        for phase, operation in teardown:
            try:
                value = operation()
                if phase == "drain":
                    transcript = value
            except BaseException as error:
                if reason is None:
                    reason = _reason(error, phase)

    if reason is None:
        classified = classify_systemd_m2(
            transcript,
            expected_debian_release=str(identity.get("debian_release", "")),
        )
        if not classified.passed:
            reason = classified.reason

    final_root_sha256: str | None = None
    if reason is None:
        try:
            final_root_sha256 = operations.hash_final_root(config, prepared)
        except BaseException as error:
            reason = _reason(error, "hash-final-root")

    result: dict[str, object] = {
        "passed": reason is None,
        "reason": reason or "pass",
        "input_sha256": dict(snapshots),
        "final_root_sha256": final_root_sha256,
        "profile": identity.get("profile"),
        "debian_release": identity.get("debian_release"),
    }
    operations.publish(config, prepared, transcript, result)
    return result


class SystemdM2Operations(ConcreteOperations):
    """Concrete adapter composed from the persistent-root gate backend."""

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        return systemd_m2_qemu_argv(**arguments)

    def invalidate(self, config: GateConfig) -> None:
        self._require_config(config)
        self._require_output().invalidate(
            "boot.ext4",
            "debian-root.run.ext2",
            "systemd-m2.serial.log",
            "result.json",
        )

    def validate_inputs(
        self, config: GateConfig, snapshots: Mapping[str, str]
    ) -> Mapping[str, object]:
        identity = dict(super().validate_inputs(config, snapshots))
        manifest = load_manifest(self.input_paths["manifest"])
        if manifest.schema_version != 2 or manifest.profile != "systemd-m2":
            raise GateFailure("rootfs manifest is not the systemd-m2 profile")
        identity["profile"] = manifest.profile
        return identity

    def launch(self, config: GateConfig, prepared: Any) -> dict[str, Any]:
        return super().launch(config, prepared, 1)

    def _boot_once(
        self, session: Mapping[str, Any], config: GateConfig, *, wait_prompt: bool
    ) -> None:
        deadline = time.monotonic() + config.boot_timeout
        serial = session["serial"]
        if wait_prompt:
            serial.wait_for(b"=> ", deadline)
        commands = (
            "virtio scan",
            "ext4load virtio 0:0 0x80200000 /asterinas.booti",
            "ext4load virtio 0:0 0x88000000 /qemu-virt.dtb",
            "fdt addr 0x88000000",
            "ext4load virtio 0:0 0x83000000 /stage1-initramfs.cpio",
            "setenv initrd_size ${filesize}",
            f'setenv bootargs "{SYSTEMD_M2_BOOTARGS}"',
        )
        for index, command in enumerate(commands, 1):
            self._send_uboot(session, command, index, deadline)
        marker = f"__ASTERINAS_SYSTEMD_BOOT_{secrets.token_hex(8).upper()}__"
        split = len(marker) // 2
        serial.send(
            (
                f"setenv ast_ba {marker[:split]}; "
                f"setenv ast_bb {marker[split:]}; "
                "echo ${ast_ba}${ast_bb}; booti 0x80200000 "
                "0x83000000:${initrd_size} 0x88000000\n"
            ).encode(),
            deadline,
        )
        serial.wait_for(marker.encode(), deadline)
        serial.wait_for(b"Starting kernel ...", deadline)

    def run_protocol(self, session: Mapping[str, Any], config: GateConfig) -> None:
        self._boot_once(session, config, wait_prompt=True)
        serial = session["serial"]
        serial.wait_for(
            b"DEBIAN_SYSTEMD_M2_READY boot=1",
            time.monotonic() + config.boot_timeout,
        )
        reboot_deadline = time.monotonic() + config.boot_timeout
        serial.wait_for(b"Hit any key to stop autoboot", reboot_deadline)
        serial.send(b" \n", reboot_deadline)
        serial.wait_for(b"=> ", reboot_deadline)
        self._boot_once(session, config, wait_prompt=False)
        serial.wait_for(
            b"DEBIAN_SYSTEMD_M2_PASS boot=2",
            time.monotonic() + config.boot_timeout,
        )

    def publish(
        self,
        config: GateConfig,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None:
        del prepared
        self._require_config(config)
        output = self._require_output()
        result["qemu_argv"] = self._attempted_argv
        output.atomic_write("systemd-m2.serial.log", transcript)
        document = json.dumps(result, indent=2, sort_keys=True) + "\n"
        output.atomic_write("result.json", document.encode())


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), SystemdM2Operations(config) as operations:
            result = orchestrate_systemd_m2_gate(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-systemd-m2-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-systemd-m2-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
