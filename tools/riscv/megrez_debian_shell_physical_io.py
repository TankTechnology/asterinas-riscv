#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Concrete serial and TFTP adapter for the Megrez Debian shell gate."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import shutil
import stat
import time
from types import SimpleNamespace
import zlib

from tools.riscv.debian.rootfs.contract import load_manifest, validate_frozen_root
from tools.riscv.debian.rootfs.gate_runtime import (
    GateProcess,
    TerminationSignalState,
    PinnedOutputDirectory,
    SerialConsole,
    launch_process,
)
from tools.riscv.megrez_board_session import (
    FINAL_MILESTONE_MARKERS,
    BoardSession,
    boot_loaded_artifacts,
    open_serial,
    run_debian_shell_phase,
    validate_recovery_epoch,
)
from tools.riscv.megrez_debian_shell_contract import (
    FrozenArtifact,
    PersistentShellPlan,
)
from tools.riscv.megrez_debian_shell_evidence import ShellPermit
from tools.riscv.megrez_debian_shell_physical import (
    PhysicalBoot,
    PhysicalShellResult,
    dnsmasq_tftp_argv,
    run_physical_gate,
)


_OUTPUT_NAMES = ("boot1.serial.log", "boot2.serial.log", "result.json")
_TFTP_FILENAMES = {
    "megrez_kernel": "kernel",
    "stage1": "stage1.cpio",
    "megrez_dtb": "megrez.dtb",
}


class TftpOnlyServer:
    """One bounded dnsmasq process offering only TFTP on an existing interface."""

    def __init__(self, interface: str, root: Path, *, launcher=launch_process) -> None:
        self.argv = dnsmasq_tftp_argv(interface, root)
        self._launcher = launcher
        self._process: GateProcess | None = None
        self._reader = -1

    def start(self, deadline: float) -> None:
        if self._process is not None:
            raise RuntimeError("TFTP server was already started")
        reader, writer = os.pipe2(os.O_CLOEXEC)
        self._reader = reader
        try:
            try:
                self._process = self._launcher(self.argv, stdio_fd=writer)
            finally:
                os.close(writer)
            SerialConsole(
                reader,
                process=self._process,
                max_bytes=64 * 1024,
            ).wait_for(b"started, version", deadline)
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        process, self._process = self._process, None
        try:
            if process is not None:
                process.terminate_group(
                    _deadline_after(3.0),
                    _deadline_after(4.0),
                )
        finally:
            if self._reader >= 0:
                os.close(self._reader)
                self._reader = -1

    def __enter__(self) -> TftpOnlyServer:
        return self

    def __exit__(self, *_error: object) -> None:
        self.stop()


class PhysicalBoardOperations:
    """Descriptor-pinned publication plus one fresh serial epoch per boot."""

    def __init__(
        self,
        *,
        device: str,
        interface: str,
        output: Path,
        board_address: str = "10.100.19.200",
        server_address: str = "10.100.19.216",
        netmask: str = "255.255.248.0",
        recovery_timeout: float = 60.0,
        launcher=launch_process,
    ) -> None:
        self.device = device
        self.interface = interface
        self.output = PinnedOutputDirectory(output)
        self.board_address = board_address
        self.server_address = server_address
        self.netmask = netmask
        self.recovery_timeout = recovery_timeout
        self.launcher = launcher
        self._temporary_root: Path | None = None
        self._server: TftpOnlyServer | None = None
        self._artifacts: dict[str, FrozenArtifact] = {}
        self._release = ""
        self._packages: tuple[tuple[str, str], ...] = ()

    def __enter__(self) -> PhysicalBoardOperations:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def invalidate(self) -> None:
        self.output.invalidate(*_OUTPUT_NAMES)

    def validate_artifacts(
        self, plan: PersistentShellPlan
    ) -> tuple[str, tuple[tuple[str, str], ...]]:
        plan.validate()
        artifacts = plan.artifact_map()
        for name, expected in artifacts.items():
            if FrozenArtifact.from_path(name, Path(expected.path)) != expected:
                raise RuntimeError(f"physical artifact changed: {name}")
        manifest = load_manifest(Path(artifacts["root_manifest"].path))
        validate_frozen_root(
            Path(artifacts["root_image"].path),
            manifest,
            Path(artifacts["packages_lock"].path),
        )
        self._release = manifest.debian_release
        self._packages = tuple(sorted(manifest.gate_packages))
        temporary_root = Path(
            os.path.realpath(
                os.path.join(
                    "/tmp", f"asterinas-megrez-tftp-{os.getpid()}-{id(self):x}"
                )
            )
        )
        temporary_root.mkdir(mode=0o755)
        self._temporary_root = temporary_root
        for name, filename in _TFTP_FILENAMES.items():
            _copy_verified(artifacts[name], temporary_root / filename)
        server = TftpOnlyServer(
            self.interface,
            temporary_root,
            launcher=self.launcher,
        )
        server.start(_deadline_after(10.0))
        self._server = server
        self._artifacts = artifacts
        return self._release, self._packages

    def run_boot(
        self, plan: PersistentShellPlan, boot_number: int, nonce: str
    ) -> PhysicalBoot:
        if self._server is None or not self._artifacts:
            raise RuntimeError("physical artifacts were not prepared")
        descriptor = open_serial(self.device)
        stream = io.StringIO()
        session = BoardSession.from_fd(
            descriptor,
            None,
            confirm=False,
            final_marker=FINAL_MILESTONE_MARKERS["debian-shell-gate"],
            log_stream=stream,
        )
        try:
            session.send("")
            session.wait_for_uboot_prompt(timeout=60.0)
            boot_loaded_artifacts(session, self._boot_arguments(plan))
            result = run_debian_shell_phase(
                session,
                boot_number=boot_number,
                nonce=nonce,
                debian_release=self._release,
                packages=self._packages,
                deadline=_deadline_after(60.0),
                reboot=True,
            )
            protocol = stream.getvalue().encode()
            recovered = False
            if result.passed:
                recovery = session.wait_for_uboot_prompt(timeout=self.recovery_timeout)
                validate_recovery_epoch(recovery)
                recovered = True
            complete = stream.getvalue().encode()
            return PhysicalBoot(protocol, complete, recovered)
        finally:
            os.close(descriptor)
            stream.close()

    def publish(self, logs: tuple[bytes, bytes], result: PhysicalShellResult) -> None:
        self._stop_server()
        self.output.atomic_write(_OUTPUT_NAMES[0], logs[0])
        self.output.atomic_write(_OUTPUT_NAMES[1], logs[1])
        self.output.atomic_write(_OUTPUT_NAMES[2], result.canonical_bytes())

    def close(self) -> None:
        try:
            self._stop_server()
        finally:
            self.output.close()
            if self._temporary_root is not None:
                shutil.rmtree(self._temporary_root)
                self._temporary_root = None

    def _stop_server(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.stop()

    def _boot_arguments(self, plan: PersistentShellPlan) -> SimpleNamespace:
        return SimpleNamespace(
            booti=_TFTP_FILENAMES["megrez_kernel"],
            initrd=_TFTP_FILENAMES["stage1"],
            dtb=_TFTP_FILENAMES["megrez_dtb"],
            bootargs=plan.gate_bootargs,
            load_transport="tftp",
            tftp_board_address=self.board_address,
            tftp_server_address=self.server_address,
            tftp_netmask=self.netmask,
            firmware_framebuffer=False,
            expected_crc32={
                "booti": self._artifacts["megrez_kernel"].crc32,
                "initrd": self._artifacts["stage1"].crc32,
                "dtb": self._artifacts["megrez_dtb"].crc32,
            },
        )


def run_physical_board_gate(
    plan: PersistentShellPlan,
    permit: ShellPermit,
    inventory: object,
    *,
    device: str,
    interface: str,
    output: Path,
) -> PhysicalShellResult:
    """Own all concrete resources while the pure two-boot state machine runs."""

    with TerminationSignalState():
        with PhysicalBoardOperations(
            device=device,
            interface=interface,
            output=output,
        ) as operations:
            return run_physical_gate(plan, permit, inventory, operations)


def _copy_verified(artifact: FrozenArtifact, destination: Path) -> None:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    source = os.open(artifact.path, flags)
    temporary = destination.with_name(f".{destination.name}.tmp")
    output = -1
    try:
        metadata = os.fstat(source)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"{artifact.name} is not a regular file")
        output = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        digest = hashlib.sha256()
        crc = 0
        size = 0
        while chunk := os.read(source, 1024 * 1024):
            _write_all(output, chunk)
            digest.update(chunk)
            crc = zlib.crc32(chunk, crc)
            size += len(chunk)
        os.fchmod(output, 0o644)
        os.fsync(output)
        if (
            size != metadata.st_size
            or os.fstat(output).st_size != size
            or size != artifact.size
            or digest.hexdigest() != artifact.sha256
            or f"{crc:08x}" != artifact.crc32
        ):
            raise RuntimeError(f"{artifact.name} changed while staging TFTP")
        os.replace(temporary, destination)
    finally:
        if output >= 0:
            os.close(output)
        os.close(source)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _deadline_after(seconds: float) -> float:
    return time.monotonic() + seconds


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise OSError("TFTP staging write made no progress")
        written += count
