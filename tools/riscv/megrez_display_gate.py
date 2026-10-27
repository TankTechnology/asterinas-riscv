#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run one watchdog-guarded physical fb0 desktop cycle.

The runner is deliberately fail-closed: it requires an already-passed QEMU
fbdev receipt and an operator-confirmed U-Boot prompt.  It never tries to
recover a RockOS shell implicitly, so a stale serial owner cannot turn into a
blind reboot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import secrets
import time

from tools.riscv import megrez_boot_menu as menu
from tools.riscv.megrez_control_chain import ControlChainError, parse_recovery_marker
from tools.riscv.megrez_menu_board import LiveLog, attach, boot_cycle
from tools.riscv.megrez_rockos_attestation import (
    RealRockOsAttestationOperations,
    _read_password,
)


FORBIDDEN_BOOT_TOKENS = (
    "drm",
    "gpu",
    "gmac",
    "dwc3",
    "firefox",
    "asterinas.net=",
    "asterinas.debian_network=",
)


class DisplayGateError(ValueError):
    pass


def validate_qemu_receipt(path: Path, *, allow_pattern_only: bool = False) -> dict:
    """Require a successful desktop-m4 QEMU receipt before touching hardware."""

    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DisplayGateError(f"invalid QEMU fbdev receipt: {error}") from error
    if receipt.get("passed") is not True or receipt.get("profile") != "desktop-m4":
        raise DisplayGateError("QEMU fbdev gate is not passed")
    pattern_only = (
        receipt.get("qualification") == "fbdev-pattern-only"
        or receipt.get("application_paint_verified") is False
    )
    if pattern_only and not allow_pattern_only:
        raise DisplayGateError("a framebuffer pattern does not qualify application painting")
    if pattern_only:
        screenshot = receipt.get("screenshot")
        if (
            not isinstance(screenshot, dict)
            or screenshot.get("distinct_sampled_colors", 0) < 2
            or screenshot.get("non_background_pixels", 0) <= 0
        ):
            raise DisplayGateError("pattern-only QEMU receipt lacks host scanout evidence")
    if not isinstance(receipt.get("input_sha256"), dict):
        raise DisplayGateError("QEMU receipt lacks immutable input identities")
    return receipt


def validate_candidate_identity(receipt: dict, document: dict) -> None:
    """Require the physical kernel and Stage1 to be the QEMU-qualified pair.

    The physical DTB and U-Boot are intentionally different artifacts from
    the QEMU run, so only the shared Asterinas payloads are compared here.
    This prevents a stale physical menu from being paired with an unrelated
    QEMU receipt while keeping the platform-specific handoff explicit.
    """

    input_hashes = receipt.get("input_sha256")
    artifacts = document.get("artifacts")
    if not isinstance(input_hashes, dict) or not isinstance(artifacts, dict):
        raise DisplayGateError("candidate identity is incomplete")
    for name in ("kernel", "stage1_initramfs"):
        item = artifacts.get(
            "kernel" if name == "kernel" else "desktop_stage1"
        )
        expected = item.get("sha256") if isinstance(item, dict) else None
        actual = input_hashes.get(name)
        if not isinstance(expected, str) or not isinstance(actual, str):
            raise DisplayGateError(f"candidate identity lacks {name}")
        if expected != actual:
            raise DisplayGateError(f"candidate {name} differs from QEMU receipt")


def validate_install_receipt(path: Path) -> dict:
    """Require proof that the framebuffer helper is on partition 2."""

    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DisplayGateError(f"invalid rootfs installation receipt: {error}") from error
    digest = receipt.get("rootfs_sha256")
    if (
        receipt.get("passed") is not True
        or receipt.get("service") != "desktop-m4-framebuffer-rw"
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise DisplayGateError("rootfs receipt does not prove the framebuffer helper")
    return receipt


def validate_display_manifest(document: dict) -> None:
    """Reject a selector that could enable unrelated hardware or networking."""

    desktop_args = document.get("desktop_args", "")
    selector = document.get("extlinux", "")
    desktop_stanza = ""
    match = re.search(
        r"(?ms)^label asterinas-desktop\n.*?^append (?P<args>[^\n]+)", selector
    )
    if match is not None:
        desktop_stanza = match.group("args")
    text = f"{desktop_args}\n{desktop_stanza}".lower()
    for token in FORBIDDEN_BOOT_TOKENS:
        if token in text:
            raise DisplayGateError(f"desktop selector contains forbidden token: {token}")
    menu.validate(document)
    if "--root-init=systemd" not in desktop_args:
        raise DisplayGateError("desktop selector does not use systemd root init")


def validate_installed_identity(installation: dict, qemu: dict) -> None:
    """Never boot a different partition-2 image under a QEMU receipt."""

    inputs = qemu.get("input_sha256")
    expected = inputs.get("root_image") if isinstance(inputs, dict) else None
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise DisplayGateError("QEMU receipt lacks the rootfs identity")
    if installation.get("rootfs_sha256") != expected:
        raise DisplayGateError("installed rootfs differs from QEMU receipt")


def validate_framebuffer_receipt(transcript: str) -> dict[str, str]:
    """Extract the one guest write/restore marker from the serial transcript."""

    normalized = transcript.replace("\r\n", "\n").replace("\r", "\n")
    pattern = re.compile(
        r"^DEBIAN_DESKTOP_M4_FB_RW width=(?P<width>[1-9][0-9]*) "
        r"height=(?P<height>[1-9][0-9]*) stride=(?P<stride>[1-9][0-9]*) "
        r"bpp=32 format=BgrReserved rect=16x16 status=pass "
        r"saved_sha256=(?P<saved>[0-9a-f]{64}) "
        r"restored_sha256=(?P<restored>[0-9a-f]{64})$",
        re.MULTILINE,
    )
    matches = tuple(pattern.finditer(normalized))
    if len(matches) != 1:
        raise DisplayGateError("physical transcript must contain one fb0 restore marker")
    result = matches[0].groupdict()
    if result["saved"] != result["restored"]:
        raise DisplayGateError("framebuffer restore hash differs from saved bytes")
    return result


def validate_recovery_marker(transcript: str, nonce: str) -> dict:
    """Require one root command response; serial reopening is a separate gate."""

    try:
        receipt = parse_recovery_marker(
            transcript, prefix="ASTERINAS_DISPLAY_RECOVERY", nonce=nonce
        )
    except ControlChainError as error:
        raise DisplayGateError(str(error)) from error
    return {"uid": receipt.uid, "boot_id": receipt.boot_id}


def run_cycle(
    *,
    manifest: Path,
    qemu_receipt: Path,
    install_receipt: Path,
    output: Path,
    device: str,
    username: str,
    password: str,
    from_uboot: bool,
    allow_pattern_only: bool = False,
) -> dict:
    """Run one physical cycle, leaving the board at the recovered prompt."""

    qemu = validate_qemu_receipt(qemu_receipt, allow_pattern_only=allow_pattern_only)
    installation = validate_install_receipt(install_receipt)
    validate_installed_identity(installation, qemu)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    validate_display_manifest(document)
    validate_candidate_identity(qemu, document)
    if not from_uboot:
        raise DisplayGateError(
            "refusing to seize a RockOS shell; rerun only after confirming a fresh U-Boot prompt"
        )
    output.mkdir(parents=True, exist_ok=False)
    nonce = secrets.token_hex(16)
    operations = attach(device)
    operations._log.close()
    operations._log = LiveLog(output / "serial.log")
    operations._require_session().log = operations._log
    started = time.monotonic()
    result: dict[str, object] = {
        "mode": "desktop",
        "run_id": nonce,
        "menu_sha256": hashlib.sha256(document["extlinux"].encode()).hexdigest(),
        "passed": False,
        "recovered": False,
        "qemu_application_paint_verified": qemu.get("application_paint_verified", True),
        "rootfs_sha256": installation["rootfs_sha256"],
    }
    try:
        cycle = boot_cycle(
            operations,
            document,
            "desktop",
            username,
            "",
            nonce,
            hardware_watchdog=True,
            # Megrez DDR/PHY training can occupy several minutes before the
            # prompt becomes observable; this is a preflight window only and
            # does not extend the guest desktop deadline.
            firmware_timeout=300,
        )
        result.update(cycle)
        # boot_cycle deliberately leaves the fresh U-Boot prompt on the old
        # descriptor.  Close it before selecting RockOS, then reopen the
        # stable device and authenticate a root command on the new descriptor.
        # This prevents a buffered pre-reboot byte from being mistaken for a
        # post-reboot control response.
        operations.close()
        operations._log.close()
        reopened = RealRockOsAttestationOperations(device)
        reopened._log = LiveLog(output / "serial-reopen.log")
        reopened.open(45)
        operations = reopened
        operations.boot_rockos(180)
        operations.login(username, password, 30)
        operations.publish(
            (
                f'printf "ASTERINAS_DISPLAY_RECOVERY nonce={nonce} uid=%s boot_id=%s\\n" '
                '"$(id -u)" "$(cat /proc/sys/kernel/random/boot_id)"',
            ),
            password,
            30,
        )
        recovery = operations._log.getvalue()
        result["rockos_control"] = validate_recovery_marker(recovery, nonce)
        result["serial_reopened"] = True
        result.update(passed=True, recovered=True)
    except BaseException as error:
        result["error"] = str(error) or type(error).__name__
    finally:
        result["elapsed_seconds"] = time.monotonic() - started
        transcript = operations._log.getvalue()
        result["serial_sha256"] = hashlib.sha256(transcript.encode()).hexdigest()
        output.joinpath("result.json").write_text(json.dumps(result, indent=2) + "\n")
        operations.close()
        operations._log.close()
    return result


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--qemu-receipt", required=True, type=Path)
    parser.add_argument("--install-receipt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--device", required=True)
    parser.add_argument("--username", default="debian")
    credentials = parser.add_mutually_exclusive_group(required=True)
    credentials.add_argument("--factory-login", action="store_true")
    credentials.add_argument("--password-fd", type=int)
    parser.add_argument(
        "--from-uboot",
        action="store_true",
        help="explicitly confirm that the exclusive serial owner is at a fresh U-Boot prompt",
    )
    parser.add_argument(
        "--allow-pattern-only",
        action="store_true",
        help="explicitly qualify only fb0 pattern scanout; do not claim application painting",
    )
    values = parser.parse_args(arguments)
    try:
        password = "debian" if values.factory_login else _read_password(values.password_fd)
        result = run_cycle(
            manifest=values.manifest,
            qemu_receipt=values.qemu_receipt,
            install_receipt=values.install_receipt,
            output=values.output,
            device=values.device,
            username=values.username,
            password=password,
            from_uboot=values.from_uboot,
            allow_pattern_only=values.allow_pattern_only,
        )
    except (OSError, ValueError, DisplayGateError) as error:
        print(f"megrez-display-gate: {error}")
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
