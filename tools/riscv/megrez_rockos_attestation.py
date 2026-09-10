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
from tools.riscv.megrez_debug_board import _lock_serial
from tools.riscv.megrez_physical_graphics import (
    HostGateError,
    _positive_seconds,
    _read_plan,
    _safe_output_directory,
)


ROCKOS_BOOT_COMMAND = "sysboot mmc 1:1 any 0x88200000 /extlinux/extlinux.conf"
ROCKOS_MENU_CHOICE = "1"
ROCKOS_PROMPT = "__ASTERINAS_ROCKOS_PROMPT__ "
MAX_ROCKOS_COMMAND_BYTES = 768
_NONCE = re.compile(r"\A[0-9a-f]{32}\Z")


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
        output_path = _safe_output_directory(self._output_directory, self._repository)
        self._output = PinnedOutputDirectory(output_path)
        self._output.invalidate(*self._OUTPUT_NAMES)

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
        operations.measure(plan, artifacts, selected_nonce, config.measurement_timeout)
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


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--mmc-kernel", required=True, type=safe_artifact_name)
    parser.add_argument("--mmc-initramfs", required=True, type=safe_artifact_name)
    parser.add_argument("--mmc-dtb", required=True, type=safe_artifact_name)
    parser.add_argument("--username", default="debian")
    parser.add_argument("--password-fd", type=int)
    parser.add_argument("--open-timeout", type=_positive_seconds, default=60.0)
    parser.add_argument("--boot-timeout", type=_positive_seconds, default=240.0)
    parser.add_argument("--login-timeout", type=_positive_seconds, default=60.0)
    parser.add_argument("--measurement-timeout", type=_positive_seconds, default=180.0)
    parser.add_argument("--recovery-timeout", type=_positive_seconds, default=180.0)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    values = argument_parser().parse_args(
        sys.argv[1:] if arguments is None else arguments
    )
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
    print(attestation.canonical_bytes().decode(), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
