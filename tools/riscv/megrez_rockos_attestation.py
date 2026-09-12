#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Create an auditable RockOS receipt for immutable Megrez MMC artifacts."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import getpass
import hashlib
import io
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from typing import Any, Protocol

from tools.riscv.debian.rootfs.gate_runtime import PinnedOutputDirectory
from tools.riscv import megrez_boot_stability as gate
from tools.riscv.megrez_board_session import (
    BoardSession,
    open_serial,
    safe_artifact_name,
)
from tools.riscv.megrez_boot_manifest import (
    ExtlinuxGeneration,
    MAX_EXTLINUX_BYTES,
    publication_manifest_bytes,
    rockos_publication_commands,
)
from tools.riscv.megrez_debug_board import _lock_serial
from tools.riscv.megrez_debug_contract import DebugContractError, DebugPlan
from tools.riscv.megrez_physical_graphics import (
    HostGateError,
    _positive_seconds,
    _read_plan,
    _safe_output_directory,
)


ROCKOS_BOOT_COMMAND = "sysboot mmc 1:1 any 0x88200000 /extlinux/extlinux.conf"
ROCKOS_MENU_CHOICE = "1"
ROCKOS_PROMPT = "__ASTERINAS_ROCKOS_PROMPT__ "
MAX_ROCKOS_COMMAND_BYTES = 1024
_NONCE = re.compile(r"\A[0-9a-f]{32}\Z")
_PUBLISH_BEGIN = re.compile(
    rb"__ASTERINAS_ROCKOS_PUBLISH_BEGIN__ nonce=([0-9a-f]{32}) "
    rb"partition=/dev/mmcblk1p1 status=0"
)
_PUBLISH_ITEM = re.compile(
    rb"__ASTERINAS_ROCKOS_PUBLISH_ITEM__ nonce=([0-9a-f]{32}) "
    rb"name=(kernel|initramfs|megrez_dtb|extlinux) status=0"
)
_PUBLISH_END = re.compile(
    rb"__ASTERINAS_ROCKOS_PUBLISH_END__ nonce=([0-9a-f]{32}) "
    rb"artifacts=3 config=1 status=0"
)


@dataclass(frozen=True)
class RockOsAttestationConfig:
    """Bounded deadlines for one maintenance-system measurement epoch."""

    open_timeout: float = 60.0
    boot_timeout: float = 240.0
    login_timeout: float = 60.0
    measurement_timeout: float = 180.0
    recovery_timeout: float = 180.0

    def __post_init__(self) -> None:
        values = (
            self.open_timeout,
            self.boot_timeout,
            self.login_timeout,
            self.measurement_timeout,
            self.recovery_timeout,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 < value <= 1200
            for value in values
        ):
            raise ValueError("RockOS attestation deadlines must be in (0, 1200]")


class RockOsAttestationOperations(Protocol):
    """Physical operations used by one controlled RockOS measurement."""

    def open(self, timeout: float) -> None: ...

    def boot_rockos(self, timeout: float) -> None: ...

    def login(self, username: str, password: str, timeout: float) -> None: ...

    def measure(
        self, plan: Any, artifacts: Mapping[str, str], nonce: str, timeout: float
    ) -> None: ...

    def reboot_and_recover(self, password: str, timeout: float) -> None: ...

    @property
    def measurement_transcript(self) -> bytes: ...

    def close(self) -> None: ...


class RockOsPublicationOperations(Protocol):
    """Physical operations used by one config-last RockOS publication."""

    def open(self, timeout: float) -> None: ...

    def boot_rockos(self, timeout: float) -> None: ...

    def login(self, username: str, password: str, timeout: float) -> None: ...

    def publish(
        self, commands: Sequence[str], password: str, timeout: float
    ) -> None: ...

    def reboot_and_recover(self, password: str, timeout: float) -> None: ...

    @property
    def publication_transcript(self) -> bytes: ...

    def close(self) -> None: ...


def measurement_commands(
    plan_sha256: str, artifacts: Mapping[str, str], nonce: str
) -> tuple[str, ...]:
    """Return shell commands whose native and framed outputs identify deployment."""

    if gate._SHA256.fullmatch(plan_sha256) is None or _NONCE.fullmatch(nonce) is None:
        raise HostGateError("RockOS measurement identity is invalid")
    if set(artifacts) != {"kernel", "initramfs", "megrez_dtb"}:
        raise HostGateError("RockOS measurement requires three MMC artifacts")
    for path in artifacts.values():
        safe_artifact_name(path)

    commands = [
        "_asterinas_partition=$(findmnt -n -o SOURCE -- /boot); "
        "_asterinas_status=$?; "
        "_asterinas_boot_id=$(cat /proc/sys/kernel/random/boot_id) || "
        "_asterinas_status=$?; "
        '[ "$_asterinas_partition" = /dev/mmcblk1p1 ] || _asterinas_status=1; '
        "printf '__ASTERINAS_ROCKOS_MEASUREMENT_BEGIN__ "
        f"nonce={nonce} plan_sha256={plan_sha256} partition=%s boot_id=%s "
        'status=%s\\n\' "$_asterinas_partition" "$_asterinas_boot_id" '
        '"$_asterinas_status"'
    ]
    for name in ("kernel", "initramfs", "megrez_dtb"):
        path = artifacts[name]
        full_path = f"/boot/{path}"
        commands.extend(
            (
                f"stat -c '%s %n' -- {full_path}",
                f"sha256sum -- {full_path}",
                f"_asterinas_file={full_path}; "
                '_asterinas_size=$(stat -c %s -- "$_asterinas_file") && '
                '_asterinas_sha_line=$(sha256sum -- "$_asterinas_file"); '
                "_asterinas_status=$?; "
                "_asterinas_sha=${_asterinas_sha_line%% *}; "
                "printf '__ASTERINAS_ROCKOS_ARTIFACT__ "
                f"nonce={nonce} name={name} mmc_path={path} size=%s "
                'sha256=%s status=%s\\n\' "$_asterinas_size" '
                '"$_asterinas_sha" "$_asterinas_status"',
            )
        )
    commands.append(
        "printf '%s\\n' '__ASTERINAS_ROCKOS_MEASUREMENT_END__ "
        f"nonce={nonce} artifacts=3 status=0'"
    )
    if any(
        len((command + "\n").encode()) > MAX_ROCKOS_COMMAND_BYTES
        for command in commands
    ):
        raise HostGateError("RockOS measurement command exceeds the safe size")
    return tuple(commands)


class RealRockOsAttestationOperations:
    """Exclusive serial adapter for the board's installed RockOS system."""

    def __init__(self, device: str) -> None:
        self._device = device
        self._fd: int | None = None
        self._session: BoardSession | None = None
        self._log = io.StringIO()
        self._measurement_start: int | None = None
        self._publication_start: int | None = None

    def open(self, timeout: float) -> None:
        fd = open_serial(self._device)
        try:
            _lock_serial(fd)
            session = BoardSession.from_fd(
                fd, None, confirm=False, log_stream=self._log
            )
            session.send("")
            session.wait_for_uboot_prompt(timeout)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        self._session = session

    def _require_session(self) -> BoardSession:
        if self._session is None:
            raise HostGateError("RockOS serial session is not open")
        return self._session

    def boot_rockos(self, timeout: float) -> None:
        session = self._require_session()
        session.command(ROCKOS_BOOT_COMMAND, expect="Enter choice:", timeout=timeout)
        session.send(ROCKOS_MENU_CHOICE)
        session.wait_for("login:", timeout)

    def login(self, username: str, password: str, timeout: float) -> None:
        if not username or not password or "\n" in username or "\n" in password:
            raise HostGateError("RockOS credentials are invalid")
        session = self._require_session()
        session.send(username)
        session.wait_for("Password:", timeout)
        session.send(password)
        session.wait_for("$ ", timeout)
        # Split the prompt literal in the echoed command, so only the resulting
        # shell prompt can satisfy the wait.
        session.send("PS1='__ASTERINAS_ROCKOS_''PROMPT__ '; export PS1")
        session.wait_for(ROCKOS_PROMPT, timeout)

    def measure(
        self, plan: Any, artifacts: Mapping[str, str], nonce: str, timeout: float
    ) -> None:
        session = self._require_session()
        self._measurement_start = len(self._log.getvalue())
        deadline = time.monotonic() + timeout
        for command in measurement_commands(plan.plan_sha256, artifacts, nonce):
            session.send(command)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("RockOS measurement deadline expired")
            session.wait_for(ROCKOS_PROMPT, remaining)

    def publish(
        self, commands: Sequence[str], password: str, timeout: float
    ) -> None:
        session = self._require_session()
        if not password or "\n" in password:
            raise HostGateError("RockOS password is invalid")
        if not commands or any(
            not isinstance(command, str)
            or len((command + "\n").encode()) > MAX_ROCKOS_COMMAND_BYTES
            for command in commands
        ):
            raise HostGateError("RockOS publication command exceeds the safe size")
        session.send("sudo -k -v")
        session.wait_for("password for", min(timeout, 30.0))
        session.send(password)
        session.wait_for(ROCKOS_PROMPT, timeout)
        self._publication_start = len(self._log.getvalue())
        deadline = time.monotonic() + timeout
        for command in commands:
            session.send(command)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("RockOS publication deadline expired")
            session.wait_for(ROCKOS_PROMPT, remaining)

    def reboot_and_recover(self, password: str, timeout: float) -> None:
        session = self._require_session()
        session.send("sudo -k reboot")
        session.wait_for("password for", min(timeout, 30.0))
        session.send(password)
        session.wait_for_uboot_prompt(timeout)

    @property
    def measurement_transcript(self) -> bytes:
        if self._measurement_start is None:
            raise HostGateError("RockOS measurement did not start")
        return self._log.getvalue()[self._measurement_start :].encode()

    @property
    def publication_transcript(self) -> bytes:
        if self._publication_start is None:
            raise HostGateError("RockOS publication did not start")
        return self._log.getvalue()[self._publication_start :].encode()

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
        self._fd = None
        self._session = None


class RealRockOsAttestationPublisher:
    """Atomically retain the raw serial source and its derived receipt."""

    _OUTPUT_NAMES = (
        "deployment-attestation.json",
        "deployment-measurement.serial.log",
        "sha256sums.txt",
    )

    def __init__(
        self,
        output_directory: Path,
        *,
        repository: Path | None = None,
    ) -> None:
        self._output_directory = output_directory
        self._repository = (
            repository.absolute()
            if repository is not None
            else Path(__file__).resolve().parents[2]
        )
        self._output: PinnedOutputDirectory | None = None

    def invalidate(self) -> None:
        if self._output is not None:
            raise HostGateError("RockOS attestation output run is already active")
        output_path = _safe_output_directory(self._output_directory, self._repository)
        output = PinnedOutputDirectory(output_path)
        try:
            output.lock_exclusive()
        except RuntimeError as error:
            output.close()
            raise HostGateError(
                "RockOS attestation output run is already active"
            ) from error
        try:
            output.invalidate(*self._OUTPUT_NAMES)
        except BaseException:
            output.close()
            raise
        self._output = output

    def close(self) -> None:
        if self._output is not None:
            self._output.close()
            self._output = None

    def __call__(
        self, attestation: gate.DeploymentAttestation, measurement: bytes
    ) -> None:
        if self._output is None:
            self.invalidate()
        output, self._output = self._output, None
        assert output is not None
        try:
            measurement_name = "deployment-measurement.serial.log"
            attestation_name = "deployment-attestation.json"
            measurement_hash = attestation.measurement_log_sha256
            attestation_payload = attestation.canonical_bytes()
            attestation_hash = hashlib.sha256(attestation_payload).hexdigest()
            output.atomic_write(measurement_name, measurement, mode=0o600)
            output.atomic_write(
                "sha256sums.txt",
                (
                    f"{measurement_hash}  {measurement_name}\n"
                    f"{attestation_hash}  {attestation_name}\n"
                ).encode(),
                mode=0o600,
            )
            output.atomic_write(attestation_name, attestation_payload, mode=0o600)
        finally:
            output.close()


class RealRockOsPublicationPublisher:
    """Atomically retain a publication transcript and generation receipt."""

    _OUTPUT_NAMES = (
        "generation.json",
        "publication.serial.log",
        "sha256sums.txt",
    )

    def __init__(
        self,
        output_directory: Path,
        *,
        repository: Path | None = None,
    ) -> None:
        self._output_directory = output_directory
        self._repository = (
            repository.absolute()
            if repository is not None
            else Path(__file__).resolve().parents[2]
        )
        self._output: PinnedOutputDirectory | None = None

    def invalidate(self) -> None:
        if self._output is not None:
            raise HostGateError("RockOS publication output run is already active")
        output_path = _safe_output_directory(
            self._output_directory, self._repository
        )
        output = PinnedOutputDirectory(output_path)
        try:
            output.lock_exclusive()
        except RuntimeError as error:
            output.close()
            raise HostGateError(
                "RockOS publication output run is already active"
            ) from error
        try:
            output.invalidate(*self._OUTPUT_NAMES)
        except BaseException:
            output.close()
            raise
        self._output = output

    def close(self) -> None:
        if self._output is not None:
            self._output.close()
            self._output = None

    def __call__(self, receipt: bytes, transcript: bytes) -> None:
        if self._output is None:
            self.invalidate()
        output, self._output = self._output, None
        assert output is not None
        try:
            log_name = "publication.serial.log"
            receipt_name = "generation.json"
            output.atomic_write(log_name, transcript, mode=0o600)
            sums = (
                f"{hashlib.sha256(transcript).hexdigest()}  {log_name}\n"
                f"{hashlib.sha256(receipt).hexdigest()}  {receipt_name}\n"
            ).encode()
            output.atomic_write("sha256sums.txt", sums, mode=0o600)
            output.atomic_write(receipt_name, receipt, mode=0o600)
        finally:
            output.close()


def _classify_publication(transcript: bytes, nonce: str) -> None:
    if not isinstance(transcript, bytes) or len(transcript) > 256 * 1024:
        raise HostGateError("RockOS publication transcript is invalid")
    normalized = transcript.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    records = []
    for line in normalized.splitlines():
        for pattern in (_PUBLISH_BEGIN, _PUBLISH_ITEM, _PUBLISH_END):
            match = pattern.fullmatch(line)
            if match is not None:
                if match.group(1).decode() != nonce:
                    raise HostGateError("RockOS publication nonce mismatch")
                records.append(line)
                break
        if line.startswith(b"__ASTERINAS_ROCKOS_PUBLISH_") and b"status=1" in line:
            raise HostGateError("RockOS publication failed")
    expected = [
        f"__ASTERINAS_ROCKOS_PUBLISH_BEGIN__ nonce={nonce} "
        "partition=/dev/mmcblk1p1 status=0",
        *(
            f"__ASTERINAS_ROCKOS_PUBLISH_ITEM__ nonce={nonce} name={name} status=0"
            for name in ("kernel", "initramfs", "megrez_dtb", "extlinux")
        ),
        f"__ASTERINAS_ROCKOS_PUBLISH_END__ nonce={nonce} "
        "artifacts=3 config=1 status=0",
    ]
    if records != [record.encode() for record in expected]:
        raise HostGateError("RockOS publication failed: incomplete ordered evidence")


def run_rockos_publication(
    plan: Any,
    generation: ExtlinuxGeneration,
    base_url: str,
    username: str,
    password: str,
    config: RockOsAttestationConfig,
    operations: RockOsPublicationOperations,
    publish: Callable[[bytes, bytes], None],
    *,
    nonce: str | None = None,
) -> bytes:
    """Publish immutable files and switch the reset config only after recovery."""

    selected_nonce = nonce if nonce is not None else secrets.token_hex(16)
    commands = rockos_publication_commands(
        generation, plan, base_url, selected_nonce
    )
    receipt = publication_manifest_bytes(generation, plan, base_url, selected_nonce)
    try:
        operations.open(config.open_timeout)
        operations.boot_rockos(config.boot_timeout)
        operations.login(username, password, config.login_timeout)
        try:
            operations.publish(commands, password, config.measurement_timeout)
        finally:
            operations.reboot_and_recover(password, config.recovery_timeout)
        transcript = operations.publication_transcript
        _classify_publication(transcript, selected_nonce)
        publish(receipt, transcript)
        return receipt
    finally:
        operations.close()


def run_rockos_attestation(
    plan: Any,
    artifacts: Mapping[str, str],
    username: str,
    password: str,
    config: RockOsAttestationConfig,
    operations: RockOsAttestationOperations,
    publish: Callable[[gate.DeploymentAttestation, bytes], None],
    *,
    nonce: str | None = None,
) -> gate.DeploymentAttestation:
    """Measure one deployment and publish only after normal board recovery."""

    plan.validate()
    selected_nonce = nonce if nonce is not None else secrets.token_hex(16)
    measurement_commands(plan.plan_sha256, artifacts, selected_nonce)
    try:
        operations.open(config.open_timeout)
        operations.boot_rockos(config.boot_timeout)
        operations.login(username, password, config.login_timeout)
        try:
            operations.measure(
                plan, artifacts, selected_nonce, config.measurement_timeout
            )
        finally:
            operations.reboot_and_recover(password, config.recovery_timeout)
        transcript = operations.measurement_transcript
        attestation = gate.DeploymentAttestation.from_measurement_log(transcript)
        if attestation.measurement_nonce != selected_nonce:
            raise HostGateError("RockOS measurement nonce mismatch")
        attestation.validate(plan, artifacts, transcript)
        publish(attestation, transcript)
        return attestation
    finally:
        operations.close()


def _read_password(descriptor: int | None) -> str:
    if descriptor is None:
        return getpass.getpass("RockOS password: ")
    if descriptor < 0:
        raise HostGateError("password descriptor is invalid")
    payload = os.read(descriptor, 258)
    if len(payload) > 257:
        raise HostGateError("RockOS password is too long")
    payload = payload.removesuffix(b"\n").removesuffix(b"\r")
    try:
        password = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HostGateError("RockOS password is not UTF-8") from error
    if not password or "\n" in password or "\r" in password or "\x00" in password:
        raise HostGateError("RockOS password is invalid")
    return password


def _read_regular(path: Path, maximum: int, label: str) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HostGateError(f"{label} is unavailable: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not 0 < metadata.st_size <= maximum
        ):
            raise HostGateError(f"{label} is not a bounded regular file")
        payload = bytearray()
        while len(payload) <= maximum:
            chunk = os.read(descriptor, maximum + 1 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) != metadata.st_size:
            raise HostGateError(f"{label} changed while being read")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _read_publication_plan(path: Path) -> DebugPlan:
    try:
        return DebugPlan.from_bytes(_read_regular(path, 64 * 1024, "debug plan"))
    except DebugContractError as error:
        raise HostGateError(f"publication debug plan is invalid: {error}") from error


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("device")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--username", default="debian")
    parser.add_argument("--password-fd", type=int)
    parser.add_argument("--open-timeout", type=_positive_seconds, default=60.0)
    parser.add_argument("--boot-timeout", type=_positive_seconds, default=240.0)
    parser.add_argument("--login-timeout", type=_positive_seconds, default=60.0)
    parser.add_argument("--measurement-timeout", type=_positive_seconds, default=180.0)
    parser.add_argument("--recovery-timeout", type=_positive_seconds, default=180.0)


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    _add_common_arguments(parser)
    parser.add_argument("--mmc-kernel", required=True, type=safe_artifact_name)
    parser.add_argument("--mmc-initramfs", required=True, type=safe_artifact_name)
    parser.add_argument("--mmc-dtb", required=True, type=safe_artifact_name)
    return parser


def publication_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish one reset-safe Megrez generation through RockOS"
    )
    parser.add_argument("action", choices=("publish",))
    _add_common_arguments(parser)
    parser.add_argument("--extlinux-config", required=True, type=Path)
    parser.add_argument("--staged-directory", required=True, type=Path)
    parser.add_argument("--base-url", required=True)
    return parser


def parse_args(arguments: Sequence[str]) -> argparse.Namespace:
    if list(arguments[:1]) == ["publish"]:
        return publication_argument_parser().parse_args(arguments)
    values = argument_parser().parse_args(arguments)
    values.action = "attest"
    return values


def main(arguments: Sequence[str] | None = None) -> int:
    values = parse_args(tuple(sys.argv[1:] if arguments is None else arguments))
    if values.action == "publish":
        publisher = RealRockOsPublicationPublisher(values.output_directory)
        try:
            publisher.invalidate()
            plan = _read_publication_plan(values.plan)
            config_bytes = _read_regular(
                values.extlinux_config,
                MAX_EXTLINUX_BYTES,
                "extlinux configuration",
            )
            generation = ExtlinuxGeneration.from_bytes(config_bytes)
            generation.validate_staged_directory(values.staged_directory, plan)
            served_config = _read_regular(
                values.staged_directory / "extlinux/asterinas.conf",
                MAX_EXTLINUX_BYTES,
                "served extlinux configuration",
            )
            if served_config != config_bytes:
                raise HostGateError(
                    "served extlinux configuration differs from the selected bytes"
                )
            password = _read_password(values.password_fd)
            config = RockOsAttestationConfig(
                open_timeout=values.open_timeout,
                boot_timeout=values.boot_timeout,
                login_timeout=values.login_timeout,
                measurement_timeout=values.measurement_timeout,
                recovery_timeout=values.recovery_timeout,
            )
            receipt = run_rockos_publication(
                plan,
                generation,
                values.base_url,
                values.username,
                password,
                config,
                RealRockOsAttestationOperations(values.device),
                publisher,
            )
        except (HostGateError, OSError, RuntimeError, TimeoutError, ValueError) as error:
            print(f"RockOS publication failed: {error}", file=sys.stderr)
            return 2
        finally:
            publisher.close()
        print(receipt.decode(), end="")
        return 0

    publisher = RealRockOsAttestationPublisher(values.output_directory)
    try:
        publisher.invalidate()
        plan = _read_plan(values.plan)
        password = _read_password(values.password_fd)
        artifacts = {
            "kernel": values.mmc_kernel,
            "initramfs": values.mmc_initramfs,
            "megrez_dtb": values.mmc_dtb,
        }
        config = RockOsAttestationConfig(
            open_timeout=values.open_timeout,
            boot_timeout=values.boot_timeout,
            login_timeout=values.login_timeout,
            measurement_timeout=values.measurement_timeout,
            recovery_timeout=values.recovery_timeout,
        )
        attestation = run_rockos_attestation(
            plan,
            artifacts,
            values.username,
            password,
            config,
            RealRockOsAttestationOperations(values.device),
            publisher,
        )
    except (HostGateError, OSError, RuntimeError, TimeoutError, ValueError) as error:
        print(f"RockOS attestation failed: {error}", file=sys.stderr)
        return 2
    finally:
        publisher.close()
    print(attestation.canonical_bytes().decode(), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
