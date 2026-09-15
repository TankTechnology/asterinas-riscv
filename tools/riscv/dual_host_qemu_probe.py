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
        if ROCKOS_CACHE_ROOT not in artifact.parents:
            raise ValueError("RockOS QEMU artifacts must be inside the probe cache")
    remote = (
        "timeout", "-k", "5", _seconds(seconds), "nice", "-n", "10",
        *_qemu(kernel, initramfs),
    )
    return (
        "ssh", "-tt", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        target, shlex.join(remote),
    )
