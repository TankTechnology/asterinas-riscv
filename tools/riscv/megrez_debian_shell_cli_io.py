#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Concrete CLI adapters for the Megrez persistent Debian shell workflow."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import subprocess
import tempfile
import time

from tools.riscv.debian.rootfs.gate_runtime import (
    PinnedOutputDirectory,
    SerialConsole,
    TerminationSignalState,
)
from tools.riscv.debian.rootfs.megrez_installer import (
    build_network_archive,
    build_verify_archive,
)
from tools.riscv.megrez_board_session import (
    FINAL_MILESTONE_MARKERS,
    BoardSession,
    boot_loaded_artifacts,
    open_serial,
    read_partition_geometry,
    validate_recovery_epoch,
)
from tools.riscv.megrez_debian_install import (
    ROOT_ARCHIVE_FILENAME,
    SERVER_ADDRESS,
    SERVER_PORT,
    _root_server,
    _run_network_install_request,
)
from tools.riscv.megrez_debian_shell_board import (
    InventoryError,
    InventoryResult,
    install_if_needed,
    run_inventory,
)
from tools.riscv.megrez_debian_shell_contract import (
    FrozenArtifact,
    PersistentShellPlan,
)
from tools.riscv.megrez_debian_shell_evidence import ShellPermit
from tools.riscv.megrez_debian_shell_physical import PhysicalShellResult
from tools.riscv.megrez_debian_shell_physical_io import (
    TftpOnlyServer,
    _copy_verified,
)
from tools.riscv.megrez_debug_contract import StageResult


_REPOSITORY = Path(__file__).resolve().parents[2]
_FILES = {
    "megrez_kernel": "kernel",
    "megrez_dtb": "megrez.dtb",
    "stage1": "stage1.cpio",
}
_READY = b"__DEBIAN_ROOTFS_SHELL_READY__"


class InventoryBoardOperations:
    """One serial epoch that measures p2 without authorizing a write."""

    def __init__(
        self,
        plan: PersistentShellPlan,
        permit: ShellPermit,
        *,
        device: str,
        interface: str,
        output: Path,
        deadline: float,
        prior_inventory: Path | None,
        install_result: Path | None,
    ) -> None:
        self.plan = plan
        self.permit = permit
        self.device = device
        self.interface = interface
        self.output = PinnedOutputDirectory(output)
        self.deadline = time.monotonic() + deadline
        self.prior_inventory = prior_inventory
        self.install_result = install_result
        self._descriptor = -1
        self._stream = io.StringIO()
        self._session: BoardSession | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._server: TftpOnlyServer | None = None

    def __enter__(self) -> InventoryBoardOperations:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def invalidate(self) -> None:
        self.output.invalidate("inventory.serial.log", "result.json")
        self._validate_artifacts()

    def read_partition_geometry(self):
        self._descriptor = open_serial(self.device)
        self._session = BoardSession.from_fd(
            self._descriptor,
            None,
            confirm=False,
            final_marker=FINAL_MILESTONE_MARKERS["verifier"],
            log_stream=self._stream,
        )
        self._session.send("")
        self._session.wait_for_uboot_prompt(timeout=self._remaining())
        return read_partition_geometry(self._session)

    def matching_install_result(
        self,
        plan: PersistentShellPlan,
        permit: ShellPermit,
        geometry: tuple[object, ...],
    ) -> str | None:
        if (self.prior_inventory is None) != (self.install_result is None):
            raise InventoryError(
                "prior inventory and install result must be supplied together"
            )
        if self.prior_inventory is None or self.install_result is None:
            return None
        prior = InventoryResult.from_bytes(_read_regular(self.prior_inventory))
        result_payload = _read_regular(self.install_result)
        result = StageResult.from_bytes(result_payload)
        permit_sha256 = hashlib.sha256(permit.canonical_bytes()).hexdigest()
        if (
            prior.status != "needs-install"
            or prior.plan_sha256 != plan.plan_sha256
            or prior.permit_sha256 != permit_sha256
            or prior.expected_root_sha256 != plan.artifact_map()["root_image"].sha256
            or prior.partitions != geometry
            or result.stage != "install"
            or result.passed is not True
            or result.reason != "install-pass"
            or result.plan_sha256 != plan.plan_sha256
        ):
            raise InventoryError("install evidence does not authorize hash reuse")
        return hashlib.sha256(result_payload).hexdigest()

    def run_verifier(self, bootargs: str) -> bytes:
        if self._session is None:
            raise InventoryError("partition geometry was not read first")
        artifacts = self.plan.artifact_map()
        self._temporary = tempfile.TemporaryDirectory(
            prefix="asterinas-megrez-inventory-"
        )
        root = Path(self._temporary.name)
        os.chmod(root, 0o755)
        verifier = root / _FILES["stage1"]
        build_verify_archive(
            Path(artifacts["installer_base"].path),
            Path(artifacts["root_image"].path),
            verifier,
            artifacts["root_image"].sha256,
        )
        os.chmod(verifier, 0o644)
        for name in ("megrez_kernel", "megrez_dtb"):
            _copy_verified(artifacts[name], root / _FILES[name])
        verifier_identity = FrozenArtifact.from_path("stage1", verifier)
        server = TftpOnlyServer(self.interface, root)
        server.start(min(self.deadline, time.monotonic() + 10.0))
        self._server = server
        arguments = SimpleNamespace(
            booti=_FILES["megrez_kernel"],
            initrd=_FILES["stage1"],
            dtb=_FILES["megrez_dtb"],
            bootargs=bootargs,
            load_transport="tftp",
            tftp_board_address="10.100.19.200",
            tftp_server_address="10.100.19.216",
            tftp_netmask="255.255.248.0",
            firmware_framebuffer=False,
            expected_crc32={
                "booti": artifacts["megrez_kernel"].crc32,
                "initrd": verifier_identity.crc32,
                "dtb": artifacts["megrez_dtb"].crc32,
            },
        )
        boot_loaded_artifacts(self._session, arguments)
        recovery = self._session.wait_for_uboot_prompt(timeout=self._remaining())
        validate_recovery_epoch(recovery)
        self._stop_server()
        return self._stream.getvalue().encode()

    def publish(self, result: InventoryResult) -> None:
        self._stop_server()
        self.output.atomic_write(
            "inventory.serial.log", self._stream.getvalue().encode()
        )
        self.output.atomic_write("result.json", result.canonical_bytes())

    def close(self) -> None:
        try:
            self._stop_server()
        finally:
            if self._descriptor >= 0:
                os.close(self._descriptor)
                self._descriptor = -1
            self._stream.close()
            self.output.close()
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None

    def _stop_server(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.stop()

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("inventory deadline expired")
        return remaining

    def _validate_artifacts(self) -> None:
        self.plan.validate()
        for name, artifact in self.plan.artifact_map().items():
            if FrozenArtifact.from_path(name, Path(artifact.path)) != artifact:
                raise InventoryError(f"inventory artifact changed: {name}")


def run_inventory_command(values: object) -> InventoryResult:
    from tools.riscv.megrez_debian_shell import (
        _load_permit,
        _prepare_directory,
        load_plan,
    )

    plan = load_plan(values.plan)
    permit = _load_permit(values.permit)
    output = _prepare_directory(values.output)
    with (
        TerminationSignalState(),
        InventoryBoardOperations(
            plan,
            permit,
            device=values.device,
            interface=values.host_interface,
            output=output,
            deadline=values.deadline,
            prior_inventory=values.prior_inventory,
            install_result=values.install_result,
        ) as operations,
    ):
        return run_inventory(plan, permit, operations)


def run_install_command(values: object) -> StageResult:
    from tools.riscv.megrez_debian_shell import (
        _load_inventory,
        _load_permit,
        _prepare_directory,
        load_plan,
    )

    plan = load_plan(values.plan)
    permit = _load_permit(values.permit)
    inventory = _load_inventory(values.inventory)
    output = _prepare_directory(values.output)

    def runner(request):
        transport = _prepare_directory(output / "transport")
        return _run_network_install_request(
            request,
            values.device,
            output,
            transport,
            f"http://{SERVER_ADDRESS}:{SERVER_PORT}/{ROOT_ARCHIVE_FILENAME}",
            build_installer=build_network_archive,
            server_factory=_root_server,
            run_command=subprocess.run,
            repository=_REPOSITORY,
            timeout=values.deadline,
        )

    return install_if_needed(
        plan,
        permit,
        inventory,
        output,
        repository=_REPOSITORY,
        run=runner,
    )


def run_handoff_command(values: object) -> None:
    from tools.riscv.megrez_debian_shell import load_plan

    plan = load_plan(values.plan)
    result = PhysicalShellResult.from_bytes(_read_regular(values.result))
    if not result.passed or result.plan_sha256 != plan.plan_sha256:
        raise InventoryError("handoff result differs from the current plan")
    for filename, expected in (
        ("boot1.serial.log", result.boot1_serial_sha256),
        ("boot2.serial.log", result.boot2_serial_sha256),
    ):
        payload = _read_regular(values.result.parent / filename)
        if hashlib.sha256(payload).hexdigest() != expected:
            raise InventoryError(f"handoff {filename} differs from the result")
    artifacts = plan.artifact_map()
    for name, artifact in artifacts.items():
        if FrozenArtifact.from_path(name, Path(artifact.path)) != artifact:
            raise InventoryError(f"handoff artifact changed: {name}")

    with (
        TerminationSignalState(),
        tempfile.TemporaryDirectory(prefix="asterinas-megrez-handoff-") as directory,
    ):
        root = Path(directory)
        os.chmod(root, 0o755)
        for name, filename in _FILES.items():
            _copy_verified(artifacts[name], root / filename)
        deadline = time.monotonic() + values.deadline
        with TftpOnlyServer(values.host_interface, root) as server:
            server.start(min(deadline, time.monotonic() + 10.0))
            descriptor = open_serial(values.device)
            stream = io.StringIO()
            try:
                session = BoardSession.from_fd(
                    descriptor,
                    None,
                    confirm=False,
                    final_marker=FINAL_MILESTONE_MARKERS["debian-shell-handoff"],
                    log_stream=stream,
                )
                session.send("")
                session.wait_for_uboot_prompt(timeout=_remaining(deadline))
                boot_loaded_artifacts(
                    session,
                    SimpleNamespace(
                        booti=_FILES["megrez_kernel"],
                        initrd=_FILES["stage1"],
                        dtb=_FILES["megrez_dtb"],
                        bootargs=plan.final_bootargs,
                        load_transport="tftp",
                        tftp_board_address="10.100.19.200",
                        tftp_server_address="10.100.19.216",
                        tftp_netmask="255.255.248.0",
                        firmware_framebuffer=False,
                        expected_crc32={
                            "booti": artifacts["megrez_kernel"].crc32,
                            "initrd": artifacts["stage1"].crc32,
                            "dtb": artifacts["megrez_dtb"].crc32,
                        },
                    ),
                )
                SerialConsole(descriptor, max_bytes=8 * 1024 * 1024).wait_for(
                    _READY, deadline
                )
            finally:
                os.close(descriptor)
                stream.close()
    print("picocom --baud 115200 --flow n --parity n --databits 8 /dev/ttyUSB0")


def _read_regular(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not 0 < metadata.st_size <= 8 * 1024 * 1024
        ):
            raise InventoryError("evidence must be a bounded regular file")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        payload = b"".join(chunks)
        if len(payload) != metadata.st_size:
            raise InventoryError("evidence changed while reading")
        return payload
    finally:
        os.close(descriptor)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("physical command deadline expired")
    return remaining
