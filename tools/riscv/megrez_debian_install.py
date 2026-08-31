#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Install one permitted Debian root over LAN while Asterinas is running."""

from __future__ import annotations

import functools
import http.server
import lzma
import os
import stat
import subprocess
import sys
import threading
import zlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
import re

from tools.riscv.debian.rootfs.megrez_installer import (
    InstallerError as RootfsInstallerError,
    _canonical_root_url,
    build_network_archive,
)
from tools.riscv.megrez_debug_contract import (
    DEBIAN_BROWSER_QUALITY_PROFILE,
    DEBIAN_BROWSER_QUALITY_SCHEMA_VERSION,
    ArtifactIdentity,
    DebugPlan,
    StageResult,
)
from tools.riscv.megrez_debug_simulation import _validate_current_artifacts
from tools.riscv.megrez_preboard import (
    PreboardPermit,
    _git_identity,
    _PinnedPermitOutput,
    _read_held,
)

RECOVERY_GRACE_SECONDS = 60.0
BOARD_STAGING_BUDGET_SECONDS = 300.0
BOARD_ADDRESS = "10.100.19.200"
SERVER_ADDRESS = "10.100.19.216"
SERVER_HARDWARE_ADDRESS = "04:7c:16:47:50:4e"
SERVER_PORT = 8080
NETMASK = "255.255.248.0"
INSTALLER_FILENAME = "debian-current-network-installer.cpio"
KERNEL_FILENAME = "asterinas-debian-current.booti.lzma"
ROOT_ARCHIVE_FILENAME = "debian-root.ext2.gz"
DTB_FILENAME = "dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb"
ArtifactValidator = Callable[[DebugPlan], dict[str, ArtifactIdentity]]
GitIdentity = Callable[[Path], str]
BuildInstaller = Callable[[Path, Path, Path, str, str], None]
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
ServerFactory = Callable[[str, int, Path], AbstractContextManager[None]]
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_CRC32_RE = re.compile(r"\A[0-9a-f]{8}\Z")
_GIT_COMMIT_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_BOOT_TOKEN_RE = re.compile(r"\A[A-Za-z0-9._,/=:+-]+\Z")


class InstallError(RuntimeError):
    """One failure that forbids or aborts the physical install attempt."""


@dataclass(frozen=True)
class NetworkInstallRequest:
    """Validated bytes and policy for one Asterinas-only root installation."""

    plan_sha256: str
    git_commit: str
    kernel: Path
    kernel_size: int
    kernel_crc32: str
    installer_base: Path
    megrez_dtb_crc32: str
    root_image: Path
    root_sha256: str
    reboot_after: int
    bootargs: str

    def validate(self) -> None:
        if _SHA256_RE.fullmatch(self.plan_sha256) is None:
            raise InstallError("install plan SHA-256 is invalid")
        if _GIT_COMMIT_RE.fullmatch(self.git_commit) is None:
            raise InstallError("install Git commit is invalid")
        _regular_file(self.kernel, "kernel")
        _regular_file(self.installer_base, "installer base")
        _regular_file(self.root_image, "root image")
        if (
            isinstance(self.kernel_size, bool)
            or not isinstance(self.kernel_size, int)
            or self.kernel_size <= 0
            or self.kernel.stat().st_size != self.kernel_size
        ):
            raise InstallError("install kernel size differs from the request")
        if _CRC32_RE.fullmatch(self.kernel_crc32) is None:
            raise InstallError("install kernel CRC32 is invalid")
        if _CRC32_RE.fullmatch(self.megrez_dtb_crc32) is None:
            raise InstallError("install Megrez DTB CRC32 is invalid")
        if _SHA256_RE.fullmatch(self.root_sha256) is None:
            raise InstallError("install root SHA-256 is invalid")
        if (
            isinstance(self.reboot_after, bool)
            or not isinstance(self.reboot_after, int)
            or not 1 <= self.reboot_after <= 3600
        ):
            raise InstallError("install reboot deadline is invalid")
        if not isinstance(self.bootargs, str) or self.bootargs != self.bootargs.strip():
            raise InstallError("install bootargs are not canonical")
        tokens = self.bootargs.split(" ")
        if any(
            not token or _BOOT_TOKEN_RE.fullmatch(token) is None for token in tokens
        ):
            raise InstallError("install bootargs contain unsafe characters")
        if tokens.count("asterinas.mmc_write_partition2") != 1 or any(
            token.startswith("asterinas.mmc_write")
            and token != "asterinas.mmc_write_partition2"
            for token in tokens
        ):
            raise InstallError("install requires the exact partition-2 write gate")
        root_token = f"asterinas.debian_install_sha256={self.root_sha256}"
        if tokens.count(root_token) != 1 or any(
            token.startswith("asterinas.debian_install_sha256=") and token != root_token
            for token in tokens
        ):
            raise InstallError("install bootargs differ from the root identity")
        reboot_token = f"asterinas.reboot_after={self.reboot_after}"
        if tokens.count(reboot_token) != 1 or any(
            token.startswith("asterinas.reboot_after=") and token != reboot_token
            for token in tokens
        ):
            raise InstallError("install bootargs differ from the recovery deadline")
        if any("/dev/mmcblk0" in token for token in tokens):
            raise InstallError("install bootargs must not name the raw MMC disk")


def _regular_file(path: object, role: str) -> None:
    if not isinstance(path, Path) or not path.is_absolute():
        raise InstallError(f"{role} path must be absolute")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InstallError(f"{role} is unavailable: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise InstallError(f"{role} must be a no-follow regular file")


class _RootServer:
    def __init__(self, address: str, port: int, directory: Path) -> None:
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(directory)
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


def _root_server(address: str, port: int, directory: Path) -> _RootServer:
    return _RootServer(address, port, directory)


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
        "asterinas.net=eic7700-rj45,10.100.19.200/21",
        (f"asterinas.neighbor=eic7700-rj45,{SERVER_ADDRESS},{SERVER_HARDWARE_ADDRESS}"),
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
    request: NetworkInstallRequest,
    installer_crc32: str,
    compressed_kernel_crc32: str,
    timeout: float,
) -> list[str]:
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
            f"booti={request.kernel_crc32},dtb={request.megrez_dtb_crc32},"
            f"initrd={installer_crc32}"
        ),
        "--load-transport",
        "ymodem",
        "--ymodem-directory",
        str(serial_directory),
        "--booti-compressed-crc32",
        compressed_kernel_crc32,
        "--booti-uncompressed-size",
        str(request.kernel_size),
        "--bootargs",
        request.bootargs,
        "--final-profile",
        "installer",
        "--milestone-timeout",
        str(int(timeout)),
        "--require-recovery",
        "--yes",
        "--log",
        str(output / "installer.serial.log"),
    ]


def _run_network_install_request(
    request: NetworkInstallRequest,
    device: str,
    output: Path,
    transport_directory: Path,
    root_url: str,
    *,
    build_installer: BuildInstaller,
    server_factory: ServerFactory,
    run_command: RunCommand,
    repository: Path,
    timeout: float | None = None,
) -> StageResult:
    """Executes one validated install request without automatic board retries."""

    request.validate()
    try:
        canonical_url = _canonical_root_url(root_url)
    except RootfsInstallerError as error:
        raise InstallError(f"install root URL is invalid: {error}") from error
    if canonical_url != (
        f"http://{SERVER_ADDRESS}:{SERVER_PORT}/{ROOT_ARCHIVE_FILENAME}"
    ):
        raise InstallError("install root URL differs from the private-LAN contract")
    milestone_timeout = (
        float(request.reboot_after) + RECOVERY_GRACE_SECONDS
        if timeout is None
        else float(timeout)
    )
    if not 0 < milestone_timeout <= 3600:
        raise InstallError("install timeout must be in (0, 3600]")
    if milestone_timeout < request.reboot_after + RECOVERY_GRACE_SECONDS:
        raise InstallError(
            "install timeout must reserve recovery grace after reboot protection"
        )

    installer = transport_directory / INSTALLER_FILENAME
    compressed_kernel = transport_directory / KERNEL_FILENAME
    _publish_lzma(request.kernel, compressed_kernel)
    try:
        build_installer(
            request.installer_base,
            request.root_image,
            installer,
            request.root_sha256,
            canonical_url,
        )
    except OSError as error:
        raise InstallError(f"cannot build Debian installer: {error}") from error
    command = _board_command(
        repository,
        device,
        output,
        transport_directory,
        request,
        _crc32(installer),
        _crc32(compressed_kernel),
        milestone_timeout,
    )
    try:
        with server_factory(SERVER_ADDRESS, SERVER_PORT, transport_directory):
            completed = run_command(
                command,
                cwd=repository,
                check=False,
                capture_output=False,
                text=True,
                timeout=milestone_timeout + BOARD_STAGING_BUDGET_SECONDS,
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
        request.plan_sha256,
        ("installer.serial.log", INSTALLER_FILENAME),
    )
    result.validate()
    return result


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
    server_factory: ServerFactory = _root_server,
    run_command: RunCommand = subprocess.run,
    repository_root: Path | None = None,
    timeout: float | None = None,
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
            if (plan.schema_version, plan.profile) not in (
                (2, "debian-browser"),
                (DEBIAN_BROWSER_QUALITY_SCHEMA_VERSION, DEBIAN_BROWSER_QUALITY_PROFILE),
            ):
                raise InstallError("install requires a supported Debian browser plan")
            if timeout is not None and (
                not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            ):
                raise InstallError("install timeout must be positive")
            milestone_timeout = (
                float(plan.reboot_after) + RECOVERY_GRACE_SECONDS
                if timeout is None
                else float(timeout)
            )
            if not 0 < milestone_timeout <= 3600:
                raise InstallError("install timeout must be in (0, 3600]")
            if milestone_timeout < plan.reboot_after + RECOVERY_GRACE_SECONDS:
                raise InstallError(
                    "install timeout must reserve recovery grace after reboot protection"
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
        request = NetworkInstallRequest(
            plan_sha256=plan.plan_sha256,
            git_commit=permit.git_commit,
            kernel=kernel,
            kernel_size=kernel.stat().st_size,
            kernel_crc32=identities["kernel"].crc32,
            installer_base=base_cpio.absolute(),
            megrez_dtb_crc32=identities["megrez_dtb"].crc32,
            root_image=Path(identities["root_image"].path),
            root_sha256=identities["root_image"].sha256,
            reboot_after=plan.reboot_after,
            bootargs=_installer_bootargs(plan, identities["root_image"].sha256),
        )
        result = _run_network_install_request(
            request,
            device,
            output,
            tftp,
            root_url,
            build_installer=build_installer,
            server_factory=server_factory,
            run_command=run_command,
            repository=repository,
            timeout=milestone_timeout,
        )
        publication.write(result.canonical_bytes())
        return result
