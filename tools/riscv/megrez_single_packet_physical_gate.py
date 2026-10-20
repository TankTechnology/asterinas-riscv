#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run exactly one watchdog-guarded UDP exchange on Megrez.

This adapter is intentionally separate from the historical 20-request HTTP
gate.  It refuses to touch the board unless the one-shot rootfs service has an
installation receipt and QEMU has already proved the packet contract.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import threading
import time
from types import SimpleNamespace

from tools.riscv.debian.rootfs.gate_runtime import PinnedOutputDirectory
from tools.riscv.megrez_control_chain import parse_recovery_marker
from tools.riscv.megrez_board_session import (
    BoardSession,
    boot_loaded_artifacts,
    validate_initial_uboot_epoch,
)
from tools.riscv.megrez_debug_board import _lock_serial, RealBoardOperations
from tools.riscv.megrez_rockos_attestation import _read_password
from tools.riscv.megrez_board_session import open_serial
from tools.riscv.megrez_single_packet_fixture import (
    OneShotUdpFixture,
    expected_summary,
)
from tools.riscv.megrez_single_packet_gate import validate_packet_exchange


BOARD_ADDRESS = "10.100.19.200"
HOST_ADDRESS = "10.100.19.216"
GATEWAY_ADDRESS = "10.100.16.1"
GATEWAY_HARDWARE_ADDRESS = "4c:d6:29:18:93:43"
HOST_HARDWARE_ADDRESS = "04:7c:16:47:50:4e"
FIXTURE_PORT = 17895


class SinglePacketPhysicalError(ValueError):
    pass


def validate_qemu_receipt(path: Path) -> dict:
    try:
        payload = path.read_text(encoding="utf-8")
        # The persistent container launcher may prefix stdout with its two
        # environment attestation lines when a receipt is copied from a run.
        # Accept only those known, non-JSON lines; arbitrary log text remains
        # invalid and cannot be mistaken for a receipt.
        lines = payload.splitlines()
        while lines and lines[0].startswith("ASTERINAS_RISCV_ROOTFS_"):
            lines.pop(0)
        receipt = json.loads("\n".join(lines))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SinglePacketPhysicalError(f"invalid QEMU packet receipt: {error}") from error
    if receipt.get("passed") is not True:
        raise SinglePacketPhysicalError("QEMU single-packet gate is not passed")
    if (
        receipt.get("tx") != 1
        or receipt.get("rx") != 1
        or receipt.get("fixture_request_count") != 1
        or receipt.get("fixture_response_count") != 1
    ):
        raise SinglePacketPhysicalError("QEMU receipt does not prove one exchange")
    if not isinstance(receipt.get("input_sha256"), dict):
        raise SinglePacketPhysicalError("QEMU receipt lacks immutable input identities")
    return receipt


def validate_install_receipt(path: Path) -> dict:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SinglePacketPhysicalError(f"invalid rootfs installation receipt: {error}") from error
    digest = receipt.get("rootfs_sha256")
    if (
        receipt.get("passed") is not True
        or receipt.get("service") != "asterinas-single-packet"
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise SinglePacketPhysicalError("rootfs receipt does not prove the single-packet service")
    return receipt


def _artifact(document: dict, name: str) -> tuple[str, str]:
    item = document.get("artifacts", {}).get(name)
    if not isinstance(item, dict) or not isinstance(item.get("path"), str):
        raise SinglePacketPhysicalError(f"manifest lacks {name} artifact")
    crc = item.get("crc32")
    if not isinstance(crc, str) or len(crc) != 8:
        raise SinglePacketPhysicalError(f"manifest lacks {name} CRC32")
    return item["path"].removeprefix("/"), crc.lower()


def physical_bootargs(nonce: str) -> str:
    if len(nonce) != 32 or any(character not in "0123456789abcdef" for character in nonce):
        raise SinglePacketPhysicalError("nonce must be 32 lowercase hexadecimal digits")
    return (
        "console=ttyS0 loglevel=info asterinas.klog_capture=info "
        "init=/init asterinas.mmc_write_partition2 "
        f"asterinas.net=eic7700-rj45,{BOARD_ADDRESS}/21,{GATEWAY_ADDRESS} "
        f"asterinas.neighbor=eic7700-rj45,{GATEWAY_ADDRESS},{GATEWAY_HARDWARE_ADDRESS} "
        f"asterinas.neighbor=eic7700-rj45,{HOST_ADDRESS},{HOST_HARDWARE_ADDRESS} "
        "asterinas.single_packet_peer=10.100.19.216 "
        f"asterinas.single_packet_nonce={nonce} "
        f"asterinas.single_packet_port={FIXTURE_PORT} "
        "systemd.setenv=ASTERINAS_DESKTOP_M5_CONSOLE=/dev/ttyS0 "
        "systemd.unit=asterinas-single-packet.service "
        "asterinas.reboot_after=90 "
        "-- --root-init=systemd"
    )


def _safe_output(path: Path) -> PinnedOutputDirectory:
    repository = Path(__file__).resolve().parents[2]
    allowed = (repository / "target").resolve()
    candidate = path.absolute()
    try:
        candidate.relative_to(allowed)
    except ValueError as error:
        raise SinglePacketPhysicalError("output must be under target/") from error
    candidate.mkdir(parents=False, mode=0o755, exist_ok=False)
    output = PinnedOutputDirectory(candidate)
    output.lock_exclusive()
    output.invalidate("single-packet.serial.log", "single-packet-fixture.json", "result.json")
    return output


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
    boot_timeout: float = 180.0,
    recovery_timeout: float = 240.0,
) -> dict[str, object]:
    """Run one physical attempt; never retries boot, packet, or recovery."""

    qemu = validate_qemu_receipt(qemu_receipt)
    installation = validate_install_receipt(install_receipt)
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SinglePacketPhysicalError(f"invalid board manifest: {error}") from error
    if not from_uboot:
        raise SinglePacketPhysicalError("fresh U-Boot confirmation is required")
    booti, booti_crc = _artifact(document, "kernel")
    initrd, initrd_crc = _artifact(document, "stage1")
    dtb, dtb_crc = _artifact(document, "dtb")
    nonce = secrets.token_hex(16)
    output_handle = _safe_output(output)
    fixture = OneShotUdpFixture(HOST_ADDRESS, FIXTURE_PORT, allowed_peer=BOARD_ADDRESS)
    log = io.StringIO()
    serial_fd: int | None = None
    session: BoardSession | None = None
    thread: threading.Thread | None = None
    fixture_summary: dict[str, object] = {}
    fixture_error: str | None = None
    result: dict[str, object] = {
        "passed": False,
        "recovered": False,
        "nonce": nonce,
        "qemu_receipt": str(qemu_receipt),
        "rootfs_install_receipt": str(install_receipt),
        "rootfs_sha256": installation["rootfs_sha256"],
    }
    started = time.monotonic()
    try:
        fixture.__enter__()
        serial_fd = open_serial(device)
        _lock_serial(serial_fd)
        session = BoardSession.from_fd(serial_fd, None, confirm=False, log_stream=log)
        os.write(serial_fd, b"\x03")
        prompt = session.wait_for_uboot_prompt(60)
        validate_initial_uboot_epoch(prompt)
        deadline = time.monotonic() + 15
        RealBoardOperations._arm_hardware_watchdog(session, deadline)
        expected_crc32 = {"booti": booti_crc, "initrd": initrd_crc, "dtb": dtb_crc}
        args = SimpleNamespace(
            load_transport="mmc",
            mmc_device_partition="1:1",
            booti=booti,
            initrd=initrd,
            dtb=dtb,
            expected_crc32=expected_crc32,
            firmware_framebuffer=False,
            bootargs=physical_bootargs(nonce),
            device_profile="full",
        )
        fixture_result: list[object] = []

        def receive() -> None:
            nonlocal fixture_error
            try:
                fixture_result.append(fixture.receive_once(nonce, boot_timeout + 15))
            except BaseException as error:  # retained in the receipt
                fixture_error = str(error) or type(error).__name__

        thread = threading.Thread(target=receive, daemon=True)
        thread.start()
        boot_loaded_artifacts(session, args)
        marker = (
            f"ASTERINAS_GMAC_SINGLE_PACKET nonce={nonce} tx=1 rx=1 "
            f"tx_errors=0 rx_errors=0 payload_sha256={expected_summary(nonce).payload_sha256} status=pass"
        )
        guest_ok = True
        try:
            session.wait_for(marker, boot_timeout)
        except TimeoutError:
            guest_ok = False
        # A pass or failure still has one and only one recovery wait.
        recovery = session.wait_for_recovery_boundary(recovery_timeout)
        if "OpenSBI" not in recovery or "U-Boot" not in recovery:
            raise SinglePacketPhysicalError("firmware recovery epoch is missing")
        if "login:" not in recovery:
            session.wait_for("login:", 180)
        session.send(username)
        session.wait_for("Password:", 30)
        session.send(password)
        session.wait_for("$ ", 30)
        session.send("PS1='__ASTERINAS_SINGLE_PACKET_ROCKOS__ '; export PS1")
        session.wait_for("__ASTERINAS_SINGLE_PACKET_ROCKOS__ ", 30)
        session.send(
            f'printf "ASTERINAS_SINGLE_PACKET_RECOVERY nonce={nonce} uid=%s boot_id=%s\\n" '
            '"$(id -u)" "$(cat /proc/sys/kernel/random/boot_id)"'
        )
        recovery_text = session.wait_for("__ASTERINAS_SINGLE_PACKET_ROCKOS__ ", 30)
        control = parse_recovery_marker(
            recovery_text, prefix="ASTERINAS_SINGLE_PACKET_RECOVERY", nonce=nonce
        )
        result["rockos_control"] = {"uid": control.uid, "boot_id": control.boot_id}
        thread.join(2)
        if fixture_result:
            fixture_summary = fixture_result[0].as_dict()
        if not guest_ok or fixture_error is not None:
            raise SinglePacketPhysicalError(fixture_error or "guest packet marker missing")
        exchange = validate_packet_exchange(log.getvalue(), fixture_summary, nonce)
        result.update(exchange, passed=True, recovered=True)
    except BaseException as error:
        result["reason"] = str(error) or type(error).__name__
    finally:
        transcript = log.getvalue().encode()
        if fixture_summary:
            output_handle.atomic_write(
                "single-packet-fixture.json",
                (json.dumps(fixture_summary, sort_keys=True) + "\n").encode(),
            )
        elif fixture_error:
            output_handle.atomic_write(
                "single-packet-fixture.json",
                (json.dumps({"status": "fail", "error": fixture_error}) + "\n").encode(),
            )
        output_handle.atomic_write("single-packet.serial.log", transcript)
        result["elapsed_seconds"] = time.monotonic() - started
        result["serial_sha256"] = hashlib.sha256(transcript).hexdigest()
        output_handle.atomic_write("result.json", (json.dumps(result, indent=2, sort_keys=True) + "\n").encode())
        if thread is not None:
            fixture.cancel()
            thread.join(2)
        if session is not None and serial_fd is not None:
            os.close(serial_fd)
        fixture.__exit__(None, None, None)
        output_handle.close()
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
    parser.add_argument("--from-uboot", action="store_true")
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
        )
    except (OSError, ValueError, SinglePacketPhysicalError) as error:
        print(f"megrez-single-packet-physical-gate: {error}")
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
