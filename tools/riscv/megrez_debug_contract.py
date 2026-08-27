# SPDX-License-Identifier: MPL-2.0

"""Immutable identities shared by Megrez simulation and physical debug runs."""

from __future__ import annotations

import hashlib
import os
import stat
import zlib
from dataclasses import dataclass
from pathlib import Path

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
ARTIFACT_NAMES = frozenset(("kernel", "initramfs", "qemu_dtb", "megrez_dtb"))


class DebugContractError(ValueError):
    """One stable launch-plan identity violation."""


@dataclass(frozen=True)
class ArtifactIdentity:
    """The bytes and load address of one debug artifact."""

    name: str
    path: str
    load_address: int
    size: int
    sha256: str
    crc32: str

    @classmethod
    def from_path(cls, name: str, path: Path, load_address: int) -> ArtifactIdentity:
        if name not in ARTIFACT_NAMES:
            raise DebugContractError("unknown artifact name")
        if (
            isinstance(load_address, bool)
            or not isinstance(load_address, int)
            or load_address <= 0
            or load_address % 4
        ):
            raise DebugContractError("load address must be positive and 4-byte aligned")

        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise DebugContractError("artifact must be a regular non-symlink file")
        if before.st_size == 0:
            raise DebugContractError("artifact is empty")
        if before.st_size > MAX_ARTIFACT_BYTES:
            raise DebugContractError("artifact exceeds the 64 MiB limit")

        digest = hashlib.sha256()
        crc = 0
        size = 0
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (before.st_dev, before.st_ino):
                raise DebugContractError("artifact identity changed before open")
            while True:
                chunk = stream.read(min(1024 * 1024, MAX_ARTIFACT_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ARTIFACT_BYTES:
                    raise DebugContractError("artifact exceeds the 64 MiB limit")
                digest.update(chunk)
                crc = zlib.crc32(chunk, crc)

        if size != opened.st_size:
            raise DebugContractError("artifact size changed while reading")
        return cls(
            name=name,
            path=str(path.absolute()),
            load_address=load_address,
            size=size,
            sha256=digest.hexdigest(),
            crc32=f"{crc:08x}",
        )
