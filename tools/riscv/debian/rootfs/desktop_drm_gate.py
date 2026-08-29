#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Gate the Debian desktop profile on a virtio-gpu DRM device."""

from __future__ import annotations

import secrets
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from tools.riscv.debian.rootfs.contract import load_manifest
from tools.riscv.debian.rootfs.desktop_m3_gate import (
    DesktopM3Operations,
    capture_rendered_ppm,
    classify_desktop,
)
from tools.riscv.debian.rootfs.gate_runtime import GateTermination, TerminationSignalState
from tools.riscv.debian.rootfs.gate_protocol import GateResult, qemu_argv
from tools.riscv.debian.rootfs.rootfs_gate import GateConfig, GateFailure, parse_gate_args
from tools.riscv.debian.rootfs.rootfs_gate_backend import ConcreteOperations, _safe_output
from tools.riscv.debian.rootfs.systemd_m2_gate import orchestrate_systemd_m2_gate


DESKTOP_DRM_BOOTARGS = "console=ttyS0 loglevel=4 init=/init -- --root-init=systemd"
DESKTOP_DRM_MILESTONES = (
    "DEBIAN_DESKTOP_DRM_UDEV state=active",
    "DEBIAN_DESKTOP_DRM_LOGIND state=active",
    "DEBIAN_DESKTOP_DRM_SESSION user=asterinas tty=tty1",
    "DEBIAN_DESKTOP_DRM_INPUT keyboard=evdev pointer=evdev",
    "DEBIAN_DESKTOP_DRM_XORG driver=modesetting device=virtio-gpu drm=active display=:0",
    "DEBIAN_DESKTOP_DRM_CLIENTS window-manager=openbox file-manager=pcmanfm panel=lxpanel terminal=xterm",
    "DEBIAN_DESKTOP_DRM_READY user=asterinas display=:0",
)


def desktop_drm_qemu_argv(**arguments: Any) -> tuple[str, ...]:
    """Return the graphical QEMU contract with virtio-gpu instead of bochs."""

    arguments.setdefault("smp", 4)
    arguments.setdefault("dtb_enabled_cpu_count", 4)
    arguments["graphical"] = True
    arguments["graphics_device"] = "virtio-gpu-device"
    return qemu_argv(**arguments)


def classify_desktop_drm(
    transcript: bytes, *, expected_debian_release: str
) -> GateResult:
    return classify_desktop(
        transcript,
        expected_debian_release=expected_debian_release,
        milestones=DESKTOP_DRM_MILESTONES,
        failure_marker=b"DEBIAN_DESKTOP_DRM_FAIL reason=",
    )


class DesktopDRMOperations(DesktopM3Operations):
    """Reuse the signed-root lifecycle while changing only display evidence."""

    SCHEMA_VERSION = 8
    PROFILE_NAME = "desktop-drm"
    ARTIFACT_PREFIX = "desktop-drm"
    MILESTONES = DESKTOP_DRM_MILESTONES
    FAILURE_MARKER = b"DEBIAN_DESKTOP_DRM_FAIL reason="
    BOOTARGS = DESKTOP_DRM_BOOTARGS

    @staticmethod
    def _qemu_argv(**arguments: Any) -> tuple[str, ...]:
        return desktop_drm_qemu_argv(**arguments)

    def invalidate(self, config: GateConfig) -> None:
        self._require_config(config)
        self._require_output().invalidate(
            "boot.ext4",
            "debian-root.run.ext2",
            f"{self.ARTIFACT_PREFIX}.serial.log",
            f"{self.ARTIFACT_PREFIX}.ppm",
            "result.json",
        )

    def validate_inputs(
        self, config: GateConfig, snapshots: Mapping[str, str]
    ) -> Mapping[str, object]:
        identity = dict(ConcreteOperations.validate_inputs(self, config, snapshots))
        manifest = load_manifest(self.input_paths["manifest"])
        if manifest.schema_version != self.SCHEMA_VERSION or manifest.profile != self.PROFILE_NAME:
            raise GateFailure("rootfs manifest is not the desktop-drm profile")
        identity["profile"] = manifest.profile
        return identity

    def _boot_commands(self) -> tuple[str, ...]:
        return (
            "virtio scan",
            "ext4load virtio 0:0 0x80200000 /asterinas.booti",
            "ext4load virtio 0:0 0x90000000 /qemu-virt.dtb",
            "fdt addr 0x90000000",
            "fdt resize 0x1000",
            "ext4load virtio 0:0 0x83000000 /stage1-initramfs.cpio",
            "setenv initrd_size ${filesize}",
            f'setenv bootargs "{self.BOOTARGS}"',
        )

    def run_protocol(self, session: dict[str, Any], config: GateConfig) -> None:
        serial = session["serial"]
        deadline = time.monotonic() + config.boot_timeout
        serial.wait_for(b"=> ", deadline)
        for index, command in enumerate(self._boot_commands(), 1):
            self._send_uboot(session, command, index, deadline)

        marker = f"__ASTERINAS_DESKTOP_DRM_BOOT_{secrets.token_hex(8).upper()}__"
        serial.send(
            (
                f"echo {marker}; booti 0x80200000 "
                "0x83000000:${initrd_size} 0x90000000\n"
            ).encode(),
            deadline,
        )
        serial.wait_for(marker.encode(), deadline)
        serial.wait_for(b"Starting kernel ...", deadline)
        completion = serial.wait_for_any(
            (self.MILESTONES[-1].encode(), self.FAILURE_MARKER),
            time.monotonic() + config.boot_timeout,
        )
        if completion.startswith(self.FAILURE_MARKER.split(b" reason=", 1)[0]):
            raise GateFailure("guest reported DRM desktop failure")

        screenshot = session["directory"] / f"{self.ARTIFACT_PREFIX}.ppm"
        self._screenshot, self._screenshot_metadata = capture_rendered_ppm(
            session["monitor"], screenshot, time.monotonic() + config.command_timeout
        )


def orchestrate_desktop_drm_gate(
    config: GateConfig, operations: DesktopDRMOperations
) -> dict[str, object]:
    return orchestrate_systemd_m2_gate(config, operations, classifier=classify_desktop_drm)


def main(arguments: list[str] | None = None) -> int:
    try:
        config = parse_gate_args(arguments)
        _safe_output(config.output_directory)
        with TerminationSignalState(), DesktopDRMOperations(config) as operations:
            result = orchestrate_desktop_drm_gate(config, operations)
        return 0 if result["passed"] else 1
    except SystemExit as error:
        return int(error.code or 0)
    except GateTermination as error:
        print(f"debian-desktop-drm-gate: terminated by signal {error.signum}", file=sys.stderr)
        return 128 + error.signum
    except BaseException as error:
        reason = error.reason if isinstance(error, GateFailure) else str(error)
        print(f"debian-desktop-drm-gate: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
