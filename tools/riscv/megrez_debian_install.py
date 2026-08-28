#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Install one permitted Debian root over LAN while Asterinas is running."""

from __future__ import annotations

import functools
import gzip
import http.server
import lzma
import os
import stat
import subprocess
import sys
import tempfile
import threading
import zlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

from tools.riscv.debian.rootfs.megrez_installer import (
    _canonical_root_url,
    build_network_archive,
)
from tools.riscv.megrez_debug_contract import ArtifactIdentity, DebugPlan, StageResult
from tools.riscv.megrez_debug_simulation import _validate_current_artifacts
from tools.riscv.megrez_preboard import (
    PreboardPermit,
    _git_identity,
    _PinnedPermitOutput,
    _read_held,
)

DEFAULT_TIMEOUT = 900.0
BOARD_ADDRESS = "10.100.19.200"
SERVER_ADDRESS = "10.100.19.216"
SERVER_PORT = 8080
NETMASK = "255.255.248.0"
INSTALLER_FILENAME = "debian-current-network-installer.cpio"
KERNEL_FILENAME = "asterinas-debian-current.booti.lzma"
ROOT_ARCHIVE_FILENAME = "debian-root.ext2.gz"
DTB_FILENAME = "dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb"
ArtifactValidator = Callable[[DebugPlan], dict[str, ArtifactIdentity]]
GitIdentity = Callable[[Path], str]
BuildInstaller = Callable[[Path, Path, Path, str, str], None]
CompressRoot = Callable[[Path, Path], None]
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
ServerFactory = Callable[[str, int, Path], AbstractContextManager[None]]


class InstallError(RuntimeError):
    """One failure that forbids or aborts the physical install attempt."""


class _RootServer:
    def __init__(self, address: str, port: int, root: Path) -> None:
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(root.parent)
        )
        try:
            self.server = http.server.ThreadingHTTPServer((address, port), handler)
        except OSError as error:
            raise InstallError(f"cannot bind Debian root server: {error}") from error
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="megrez-debian-root-server",
            daemon=True,
        )

    def __enter__(self) -> None:
        self.thread.start()

    def __exit__(self, *_error: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _root_server(address: str, port: int, root: Path) -> _RootServer:
    return _RootServer(address, port, root)


def _safe_directory(path: Path, *, repository: Path) -> Path:
    candidate = path.absolute()
    try:
        candidate.relative_to(repository / "target")
    except ValueError as error:
        raise InstallError(
            "install directory must stay below repository target"
        ) from error
    current = repository
    for component in candidate.relative_to(repository).parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise InstallError("install directory contains an unsafe component")
    candidate.mkdir(parents=True, mode=0o755, exist_ok=True)
    return candidate


def _publish_lzma(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.tmp.{os.getpid()}")
    try:
        compressed = lzma.compress(
            source.read_bytes(), format=lzma.FORMAT_ALONE, preset=9
        )
        with temporary.open("xb") as output_stream:
            output_stream.write(compressed)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_gzip(source: Path, destination: Path) -> None:
    """Atomically publish one deterministic streaming gzip transport."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            with source.open("rb") as input_stream:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    compresslevel=1,
                    fileobj=output,
                    mtime=0,
                ) as compressor:
                    while chunk := input_stream.read(1024 * 1024):
                        compressor.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
        temporary = None
        directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _crc32(path: Path) -> str:
    crc = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            crc = zlib.crc32(chunk, crc)
    return f"{crc & 0xFFFF_FFFF:08x}"


def _installer_bootargs(plan: DebugPlan, root_hash: str) -> str:
    tokens = [
        "console=tty0",
        "console=ttyS0",
        "cpu_no_boost_1_6ghz",
        "loglevel=info",
        "init=/init",
        "asterinas.net=eic7700-rj45,10.100.19.200/21,10.100.16.1",
        "asterinas.mmc_write_partition2",
        f"asterinas.debian_install_sha256={root_hash}",
        f"asterinas.reboot_after={plan.reboot_after}",
    ]
    return " ".join(tokens)


def _validate_permit(
    plan: DebugPlan,
    permit: PreboardPermit,
    identities: dict[str, ArtifactIdentity],
    commit: str,
) -> None:
    expected_transfers = tuple(
        (name, identities[name].crc32) for name in ("kernel", "initramfs", "megrez_dtb")
    )
    if (
        permit.plan_sha256 != plan.plan_sha256
        or permit.kernel_sha256 != identities["kernel"].sha256
        or permit.git_commit != commit
        or permit.transfer_crc32 != expected_transfers
        or permit.bootargs != plan.bootargs
        or permit.reboot_after != plan.reboot_after
    ):
        raise InstallError("preboard permit does not match current inputs")


def _board_command(
    repository: Path,
    device: str,
    output: Path,
    serial_directory: Path,
    permit: PreboardPermit,
    installer_crc32: str,
    compressed_kernel_crc32: str,
    kernel_size: int,
    bootargs: str,
    timeout: float,
) -> list[str]:
    transfers = dict(permit.transfer_crc32)
    return [
        sys.executable,
        str(repository / "tools/riscv/megrez_board_session.py"),
        device,
        "--booti",
        KERNEL_FILENAME,
        "--dtb",
        DTB_FILENAME,
        "--initrd",
        INSTALLER_FILENAME,
        "--expected-crc32",
        (
            f"booti={transfers['kernel']},dtb={transfers['megrez_dtb']},"
            f"initrd={installer_crc32}"
        ),
        "--load-transport",
        "ymodem",
        "--ymodem-directory",
        str(serial_directory),
        "--booti-compressed-crc32",
        compressed_kernel_crc32,
        "--booti-uncompressed-size",
        str(kernel_size),
        "--bootargs",
        bootargs,
        "--final-profile",
        "installer",
        "--milestone-timeout",
        str(int(timeout)),
        "--require-recovery",
        "--yes",
        "--log",
        str(output / "installer.serial.log"),
    ]


def run_network_install(
    plan: DebugPlan,
    permit_path: Path,
    device: str,
    output_directory: Path,
    base_cpio: Path,
    tftp_directory: Path,
    root_url: str,
    *,
    artifact_validator: ArtifactValidator = _validate_current_artifacts,
    git_identity: GitIdentity = _git_identity,
    build_installer: BuildInstaller = build_network_archive,
    compress_root: CompressRoot = _publish_gzip,
    server_factory: ServerFactory = _root_server,
    run_command: RunCommand = subprocess.run,
    repository_root: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> StageResult:
    """Build and run one permit-bound Asterinas-only LAN install."""

    repository = (
        repository_root.absolute()
        if repository_root is not None
        else Path(__file__).resolve().parents[2]
    )
    output = _safe_directory(output_directory, repository=repository)
    with _PinnedPermitOutput(output / "result.json", repository) as publication:
        publication.invalidate()
        try:
            plan.validate()
            if plan.schema_version != 2 or plan.profile != "debian-browser":
                raise InstallError("install requires a Debian browser plan")
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
                raise InstallError("install timeout must be positive")
            if not 0 < float(timeout) <= 3600:
                raise InstallError("install timeout must be in (0, 3600]")
            canonical_url = _canonical_root_url(root_url)
            if canonical_url != (
                f"http://{SERVER_ADDRESS}:{SERVER_PORT}/{ROOT_ARCHIVE_FILENAME}"
            ):
                raise InstallError(
                    "install root URL differs from the private-LAN contract"
                )
            permit_payload, _permit_hash = _read_held(
                permit_path, label="preboard-permit"
            )
            permit = PreboardPermit.from_bytes(permit_payload)
            identities = artifact_validator(plan)
            _validate_permit(plan, permit, identities, git_identity(repository))
            if artifact_validator(plan) != identities:
                raise InstallError("install artifacts changed during validation")
        except InstallError:
            raise
        except Exception as error:
            raise InstallError(f"install validation failed: {error}") from error

        tftp = _safe_directory(tftp_directory, repository=repository)
        kernel = Path(identities["kernel"].path)
        root = Path(identities["root_image"].path)
        compressed_root = tftp / ROOT_ARCHIVE_FILENAME
        installer = tftp / INSTALLER_FILENAME
        compressed_kernel = tftp / KERNEL_FILENAME
        try:
            compress_root(root, compressed_root)
        except OSError as error:
            raise InstallError(f"cannot compress Debian root: {error}") from error
        _publish_lzma(kernel, compressed_kernel)
        build_installer(
            base_cpio,
            root,
            installer,
            identities["root_image"].sha256,
            canonical_url,
        )
        bootargs = _installer_bootargs(plan, identities["root_image"].sha256)
        command = _board_command(
            repository,
            device,
            output,
            tftp,
            permit,
            _crc32(installer),
            _crc32(compressed_kernel),
            kernel.stat().st_size,
            bootargs,
            float(timeout),
        )
        try:
            with server_factory(SERVER_ADDRESS, SERVER_PORT, compressed_root):
                completed = run_command(
                    command,
                    cwd=repository,
                    check=False,
                    capture_output=False,
                    text=True,
                    timeout=float(timeout) + 120,
                )
        except subprocess.TimeoutExpired as error:
            raise InstallError("board install timed out") from error
        except OSError as error:
            raise InstallError(f"cannot launch board install: {error}") from error
        if completed.returncode != 0:
            raise InstallError(f"board install failed: exit {completed.returncode}")
        result = StageResult(
            1,
            "install",
            True,
            "install-pass",
            plan.plan_sha256,
            ("installer.serial.log", INSTALLER_FILENAME),
        )
        result.validate()
        publication.write(result.canonical_bytes())
        return result
