#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Cold-boot the schema-six Debian Firefox profile under the formal M5 NIC."""

from __future__ import annotations

import sys
from typing import Any

from tools.riscv.debian.rootfs.desktop_m3_gate import classify_desktop
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    DESKTOP_M5_QEMU_MILESTONES,
)
from tools.riscv.debian.rootfs.desktop_m5_qemu_gate import (
    DESKTOP_M5_QEMU_BOOTARGS,
    DesktopM5QemuOperations,
    desktop_m5_qemu_argv,
)
from tools.riscv.debian.rootfs.gate_protocol import GateResult
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


BROWSER_M5_MILESTONES = (
    "DEBIAN_BROWSER_M5_NETNS firefox=private initial=distinct",
    "DEBIAN_BROWSER_M5_UDEV state=active",
    "DEBIAN_BROWSER_M5_LOGIND state=active",
    "DEBIAN_BROWSER_M5_SESSION user=asterinas tty=tty1",
    "DEBIAN_BROWSER_M5_INPUT keyboard=evdev pointer=evdev",
    "DEBIAN_BROWSER_M5_XORG framebuffer=fbdev display=:0",
    "DEBIAN_BROWSER_M5_CLIENTS window-manager=matchbox browser=firefox-esr terminal=xterm",
    "DEBIAN_BROWSER_M5_WORKLOAD mode=offline scheme=file network=private-loopback",
    "DEBIAN_BROWSER_M5_CONTENT js=pass media=vp8-webm canplay=pass ended=pass "
    "network_mode=private-loopback source=file direct_nonloopback_ip=unavailable",
    "DEBIAN_BROWSER_M5_READY user=asterinas display=:0",
)
BROWSER_M5_QEMU_MILESTONES = (*DESKTOP_M5_QEMU_MILESTONES, *BROWSER_M5_MILESTONES)
BROWSER_M5_QEMU_BOOTARGS = DESKTOP_M5_QEMU_BOOTARGS
_NETWORK_FAILURE = b"DEBIAN_NETWORK_M5_FAIL reason="
_NETNS_FAILURE = b"DEBIAN_BROWSER_M5_NETNS_FAIL reason="
_BROWSER_FAILURE = b"DEBIAN_BROWSER_M5_FAIL reason="


def browser_m5_qemu_argv(**arguments: Any) -> tuple[str, ...]:
    """Reuse the one-slirp-NIC graphical contract admitted by network M5."""

    return desktop_m5_qemu_argv(**arguments)


def classify_browser_m5_qemu(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    """Require host networking and private-loopback Firefox content evidence."""

    clean = transcript.lower()
    if _NETWORK_FAILURE.lower() in clean:
        return GateResult(False, "network guest failure", None)
    if _NETNS_FAILURE.lower() in clean:
        return GateResult(False, "browser namespace failure", None)
    return classify_desktop(
        transcript,
        expected_debian_release=expected_debian_release,
        milestones=BROWSER_M5_QEMU_MILESTONES,
        failure_marker=_BROWSER_FAILURE,
    )


class BrowserM5QemuOperations(DesktopM5QemuOperations):
    """Bind the formal M5 graphical/network lifecycle to browser schema six."""

    SCHEMA_VERSION = 6
    PROFILE_NAME = "browser-m5"
    ARTIFACT_PREFIX = "browser-m5-qemu"
    MILESTONES = BROWSER_M5_QEMU_MILESTONES
    FAILURE_MARKER = _BROWSER_FAILURE
    ADDITIONAL_FAILURE_MARKERS = (_NETWORK_FAILURE, _NETNS_FAILURE)
    BOOTARGS = BROWSER_M5_QEMU_BOOTARGS

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        return browser_m5_qemu_argv(**arguments)


def orchestrate_browser_m5_qemu_gate(
    config: GateConfig, operations: BrowserM5QemuOperations
) -> dict[str, object]:
    """Run the immutable browser root with the existing bounded backend."""

    return orchestrate_systemd_m2_gate(
        config,
        operations,
        classifier=classify_browser_m5_qemu,
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), BrowserM5QemuOperations(config) as operations:
            result = orchestrate_browser_m5_qemu_gate(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-browser-m5-qemu-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-browser-m5-qemu-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
