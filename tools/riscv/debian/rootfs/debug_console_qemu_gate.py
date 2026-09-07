#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Gate the opt-in root serial console through the graphical QEMU lifecycle."""

from __future__ import annotations

import secrets
import sys
import time
from dataclasses import asdict
from typing import Any, Mapping

from tools.riscv.debian.rootfs.debug_console_protocol import (
    DebugConsoleEvidence,
    DebugConsoleProtocolError,
    run_debug_console_phase,
)
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    classify_desktop_m5_qemu,
)
from tools.riscv.debian.rootfs.desktop_m5_qemu_gate import (
    DESKTOP_M5_QEMU_BOOTARGS,
    DesktopM5QemuOperations,
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
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output
from tools.riscv.debian.rootfs.systemd_m2_gate import orchestrate_systemd_m2_gate


class DebugConsoleQemuOperations(DesktopM5QemuOperations):
    """Add fixed root-shell probes to the established M5 desktop gate."""

    ARTIFACT_PREFIX = "debug-root-console"
    BOOTARGS = DESKTOP_M5_QEMU_BOOTARGS + " --debug-console=root"

    def __init__(self, config: GateConfig) -> None:
        self.debug_evidence: DebugConsoleEvidence | None = None
        super().__init__(config)

    @classmethod
    def _accepted_profile_identities(cls) -> tuple[tuple[int, str], ...]:
        return (
            (5, "desktop-m5-network"),
            (7, "browser-web"),
        )

    def run_protocol(self, session: Mapping[str, Any], config: GateConfig) -> None:
        super().run_protocol(session, config)
        nonce = secrets.token_hex(16)
        try:
            self.debug_evidence = run_debug_console_phase(
                session["serial"],
                time.monotonic() + config.command_timeout,
                nonce,
            )
        except (
            DebugConsoleProtocolError,
            TimeoutError,
            BufferError,
            EOFError,
            UnicodeError,
        ) as error:
            raise GateFailure(f"debug-console protocol failed: {error}") from error

    def publish(
        self,
        config: GateConfig,
        prepared: Any,
        transcript: bytes,
        result: dict[str, object],
    ) -> None:
        result["debug_console"] = (
            asdict(self.debug_evidence) if self.debug_evidence is not None else None
        )
        super().publish(config, prepared, transcript, result)


def orchestrate_debug_console_qemu_gate(
    config: GateConfig, operations: DebugConsoleQemuOperations
) -> dict[str, object]:
    """Run the one-boot graphical lifecycle with debug-console evidence."""

    return orchestrate_systemd_m2_gate(
        config,
        operations,
        classifier=classify_desktop_m5_qemu,
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DebugConsoleQemuOperations(
            config
        ) as operations:
            result = orchestrate_debug_console_qemu_gate(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-debug-console-qemu-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-debug-console-qemu-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
