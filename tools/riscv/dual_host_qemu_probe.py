#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run the same diskless Stage1 probe on developer QEMU and RockOS TCG."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
from typing import Protocol

from tools.riscv.megrez_probe import qemu_probe_argv


MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
ROCKOS_CACHE_ROOT = Path("/tmp/asterinas-qemu-probe")
_SAFE_TARGET = re.compile(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+")
_SAFE_CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


@dataclass(frozen=True)
class ArtifactIdentity:
    size: int
    sha256: str


def read_identity(path: Path) -> ArtifactIdentity:
    """Pin and hash one bounded regular artifact without following a symlink."""

    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_ARTIFACT_BYTES:
            raise ValueError("artifact must be a nonempty regular file below 64 MiB")
        digest = hashlib.sha256()
        observed_size = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            observed_size += len(chunk)
            if observed_size > MAX_ARTIFACT_BYTES:
                raise ValueError("artifact exceeds 64 MiB")
            digest.update(chunk)
        if observed_size != metadata.st_size:
            raise ValueError("artifact changed during hashing")
        return ArtifactIdentity(observed_size, digest.hexdigest())
    finally:
        os.close(descriptor)


def qemu_bootargs() -> str:
    """Select only the fixed Stage1 probe, with a bounded guest reboot."""

    return (
        "console=ttyS0 loglevel=info asterinas.klog_capture=info "
        "init=/init asterinas.reboot_after=180 -- --root-init=probe"
    )


def _seconds(value: int) -> str:
    if type(value) is not int or not 10 <= value <= 600:
        raise ValueError("QEMU timeout must be an integer from 10 to 600 seconds")
    return str(value)


def _qemu(kernel: Path, initramfs: Path) -> tuple[str, ...]:
    return (*qemu_probe_argv(kernel, initramfs, qemu_bootargs()), "-accel", "tcg")


def developer_argv(
    container: str, kernel: Path, initramfs: Path, seconds: int
) -> tuple[str, ...]:
    """Bound QEMU inside the current persistent developer container."""

    if _SAFE_CONTAINER.fullmatch(container) is None:
        raise ValueError("unsafe developer container name")
    return (
        "docker", "exec", "-it", "--workdir", "/root/asterinas", container,
        "timeout", "-k", "5", _seconds(seconds), *_qemu(kernel, initramfs),
    )


def rockos_argv(
    target: str, kernel: Path, initramfs: Path, seconds: int
) -> tuple[str, ...]:
    """Bound a low-priority TCG guest on RockOS through one SSH PTY."""

    if _SAFE_TARGET.fullmatch(target) is None:
        raise ValueError("unsafe RockOS SSH target")
    for artifact in (kernel, initramfs):
        _remote_path(artifact)
    remote = (
        "timeout", "-k", "5", _seconds(seconds), "nice", "-n", "10",
        *_qemu(kernel, initramfs),
    )
    return (
        "ssh", "-tt", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        target, shlex.join(remote),
    )


def cache_directory(
    kernel: ArtifactIdentity, initramfs: ArtifactIdentity
) -> Path:
    """Name one immutable temporary cache for an exact artifact pair."""

    for identity in (kernel, initramfs):
        if identity.size <= 0 or re.fullmatch(r"[0-9a-f]{64}", identity.sha256) is None:
            raise ValueError("invalid QEMU artifact identity")
    return ROCKOS_CACHE_ROOT / f"{kernel.sha256[:16]}-{initramfs.sha256[:16]}"


def _remote_path(path: Path) -> str:
    if ROCKOS_CACHE_ROOT not in path.parents or any(
        part in {".", ".."} or re.fullmatch(r"[A-Za-z0-9._-]+", part) is None
        for part in path.relative_to(ROCKOS_CACHE_ROOT).parts
    ):
        raise ValueError("unsafe RockOS probe cache path")
    return shlex.quote(str(path))


def _ssh_argv(target: str, command: str) -> tuple[str, ...]:
    if _SAFE_TARGET.fullmatch(target) is None:
        raise ValueError("unsafe RockOS SSH target")
    return (
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=5", target, command,
    )


def remote_identity_argv(target: str, path: Path) -> tuple[str, ...]:
    """Render a no-follow size and SHA-256 check for one RockOS cache file."""

    quoted = _remote_path(path)
    command = (
        f"if test -L {quoted}; then exit 3; fi; "
        f"if test ! -e {quoted}; then exit 4; fi; "
        f"test -f {quoted} || exit 3; "
        f"stat -c %s -- {quoted}; sha256sum -- {quoted}"
    )
    return _ssh_argv(target, command)


class ArtifactTransport(Protocol):
    def ensure_directory(self, path: Path) -> None: ...
    def identity(self, path: Path) -> ArtifactIdentity | None: ...
    def copy(self, source: Path, destination: Path) -> None: ...
    def promote(self, partial: Path, destination: Path) -> None: ...
    def remove_partial(self, path: Path) -> None: ...


def stage_artifact(
    transport: ArtifactTransport,
    source: Path,
    expected: ArtifactIdentity,
    destination: Path,
    *,
    nonce: str,
) -> Path:
    """Reuse verified cached bytes or promote one verified temporary transfer."""

    _remote_path(destination)
    if re.fullmatch(r"[0-9a-f]{16}", nonce) is None:
        raise ValueError("invalid transfer nonce")
    transport.ensure_directory(destination.parent)
    observed = transport.identity(destination)
    if observed is not None:
        if observed != expected:
            raise ValueError("cached artifact identity mismatch")
        return destination
    partial = destination.with_name(f".{destination.name}.{nonce}.part")
    try:
        transport.copy(source, partial)
        if transport.identity(partial) != expected:
            raise ValueError("transferred artifact identity mismatch")
        transport.promote(partial, destination)
        if transport.identity(destination) != expected:
            raise ValueError("promoted artifact identity mismatch")
    finally:
        transport.remove_partial(partial)
    return destination


class RockOsArtifactTransport:
    """Bounded SSH/SCP operations confined to the temporary probe cache."""

    def __init__(self, target: str, *, operation_seconds: int = 60) -> None:
        if _SAFE_TARGET.fullmatch(target) is None:
            raise ValueError("unsafe RockOS SSH target")
        self.target = target
        self.operation_seconds = int(_seconds(operation_seconds))

    def _run(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                argv, capture_output=True, check=False, timeout=self.operation_seconds
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("RockOS artifact operation timed out") from error

    def _ssh(self, command: str) -> None:
        result = self._run(_ssh_argv(self.target, command))
        if result.returncode != 0:
            raise RuntimeError(
                f"RockOS cache command exited {result.returncode}: "
                f"{result.stderr[:256].decode(errors='replace')}"
            )

    def ensure_directory(self, path: Path) -> None:
        quoted = _remote_path(path)
        root = shlex.quote(str(ROCKOS_CACHE_ROOT))
        self._ssh(
            f"test ! -L {root} || exit 3; mkdir -p -- {quoted}; "
            f"test ! -L {quoted} || exit 3"
        )

    def identity(self, path: Path) -> ArtifactIdentity | None:
        result = self._run(remote_identity_argv(self.target, path))
        if result.returncode == 4:
            return None
        if result.returncode != 0:
            raise RuntimeError(
                f"RockOS identity check exited {result.returncode}: "
                f"{result.stderr[:256].decode(errors='replace')}"
            )
        lines = result.stdout.decode("ascii", errors="strict").splitlines()
        if len(lines) != 2 or re.fullmatch(r"[0-9]+", lines[0]) is None:
            raise ValueError("malformed RockOS artifact identity")
        checksum = lines[1].split("  ", 1)
        if (
            len(checksum) != 2
            or checksum[1] != str(path)
            or re.fullmatch(r"[0-9a-f]{64}", checksum[0]) is None
        ):
            raise ValueError("malformed RockOS SHA-256 output")
        return ArtifactIdentity(int(lines[0]), checksum[0])

    def copy(self, source: Path, destination: Path) -> None:
        _remote_path(destination)
        result = self._run(
            (
                "scp", "-q", "-o", "BatchMode=yes", "-o",
                "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5",
                str(source), f"{self.target}:{destination}",
            )
        )
        if result.returncode != 0:
            raise RuntimeError(f"RockOS transfer exited {result.returncode}")

    def promote(self, partial: Path, destination: Path) -> None:
        self._ssh(f"mv -n -- {_remote_path(partial)} {_remote_path(destination)}")

    def remove_partial(self, path: Path) -> None:
        self._ssh(f"rm -f -- {_remote_path(path)}")
