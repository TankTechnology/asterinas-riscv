#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Cold-boot the schema-seven real-web Firefox profile with one slirp NIC."""

from __future__ import annotations

import sys
from typing import Any

from tools.riscv.debian.rootfs.desktop_m3_gate import classify_desktop
from tools.riscv.debian.rootfs.desktop_m5_qemu_gate import (
    DESKTOP_M5_QEMU_BOOTARGS,
    DesktopM5QemuOperations,
    desktop_m5_qemu_argv,
)
from tools.riscv.debian.rootfs.gate_protocol import GateResult
from tools.riscv.debian.rootfs.gate_runtime import GateTermination, TerminationSignalState
from tools.riscv.debian.rootfs.rootfs_gate import GateConfig, GateFailure, parse_gate_args
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output
from tools.riscv.debian.rootfs.systemd_m2_gate import orchestrate_systemd_m2_gate


BROWSER_WEB_MILESTONES = (
    "DEBIAN_BROWSER_WEB_NETWORK nic=virtio-slirp dns=10.0.2.3 https=curl-verified",
    "DEBIAN_BROWSER_WEB_TRUST ca=system nss=libnssckbi p11=p11-kit-trust cert_override=absent",
    "DEBIAN_BROWSER_WEB_SECURITY parent_uid=1000 caps=zero nnp=1 content_seccomp=2 sandbox=normal",
    "DEBIAN_BROWSER_WEB_CONTENT baidu_home=pass baidu_search=pass bilibili_home=pass bilibili_detail=pass bv=BV",
    "DEBIAN_BROWSER_WEB_READY user=asterinas display=:0",
)
_NETWORK_FAILURE = b"DEBIAN_NETWORK_M5_FAIL reason="
_WEB_FAILURE = b"DEBIAN_BROWSER_WEB_FAIL reason="


def browser_web_qemu_argv(**arguments: Any) -> tuple[str, ...]:
    """Admit exactly one default slirp backend and one virtio-net transport."""

    argv = desktop_m5_qemu_argv(**arguments)
    root_drives = [
        (index, value)
        for index, value in enumerate(argv)
        if value.startswith("if=none,format=raw,file=")
        and ",id=rootdisk,cache=directsync" in value
    ]
    if len(root_drives) != 1:
        raise ValueError("online web runner requires one writable run-copy disk")
    root_index, root_drive = root_drives[0]
    argv = (
        *argv[:root_index],
        root_drive.replace(",cache=directsync", ",cache=writeback"),
        *argv[root_index + 1 :],
    )
    if argv.count("-netdev") != 1 or argv.count("user,id=net0") != 1:
        raise ValueError("online web runner requires exactly one slirp backend")
    if argv.count("-device") < 1 or argv.count("virtio-net-device,netdev=net0") != 1:
        raise ValueError("online web runner requires exactly one virtio NIC")
    if "-nic" in argv:
        raise ValueError("online web runner contains a conflicting NIC contract")
    return argv


def classify_browser_web_qemu(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    clean = transcript.lower()
    if _NETWORK_FAILURE.lower() in clean:
        return GateResult(False, "network guest failure", None)
    return classify_desktop(
        transcript,
        expected_debian_release=expected_debian_release,
        milestones=BROWSER_WEB_MILESTONES,
        failure_marker=_WEB_FAILURE,
    )


class BrowserWebQemuOperations(DesktopM5QemuOperations):
    SCHEMA_VERSION = 7
    PROFILE_NAME = "browser-web"
    ARTIFACT_PREFIX = "browser-web-qemu"
    MILESTONES = BROWSER_WEB_MILESTONES
    FAILURE_MARKER = _WEB_FAILURE
    ADDITIONAL_FAILURE_MARKERS = (_NETWORK_FAILURE,)
    BOOTARGS = DESKTOP_M5_QEMU_BOOTARGS

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        return browser_web_qemu_argv(**arguments)


def orchestrate_browser_web_qemu_gate(
    config: GateConfig, operations: BrowserWebQemuOperations
) -> dict[str, object]:
    return orchestrate_systemd_m2_gate(
        config, operations, classifier=classify_browser_web_qemu
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), BrowserWebQemuOperations(config) as operations:
            result = orchestrate_browser_web_qemu_gate(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(
            f"debian-browser-web-qemu-gate: terminated by signal {error.signum}",
            file=sys.stderr,
        )
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-browser-web-qemu-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
