#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Strict reset-safe extlinux generations for the Megrez board."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any
import zlib


MAX_EXTLINUX_BYTES = 16 * 1024
ARTIFACT_ORDER = ("kernel", "initramfs", "megrez_dtb")
_DIRECTIVE_TO_ARTIFACT = {
    "linux": "kernel",
    "initrd": "initramfs",
    "fdt": "megrez_dtb",
}
_DIRECTIVE_ORDER = ("default", "label", "linux", "initrd", "fdt", "append")
_SAFE_LABEL = re.compile(r"[a-z0-9][a-z0-9.-]*")
_SAFE_PATH = re.compile(
    r"/[A-Za-z0-9][A-Za-z0-9._+-]*(?:/[A-Za-z0-9][A-Za-z0-9._+-]*)*"
)


class BootManifestError(ValueError):
    """One extlinux generation is unsafe, stale, or internally inconsistent."""


@dataclass(frozen=True)
class ExtlinuxGeneration:
    """The deterministic extlinux subset used by the Megrez Asterinas entry."""

    default: str
    label: str
    linux: str
    initrd: str
    fdt: str
    append: str

    @classmethod
    def from_bytes(cls, data: bytes) -> ExtlinuxGeneration:
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_EXTLINUX_BYTES:
            raise BootManifestError("extlinux configuration has an invalid size")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise BootManifestError("extlinux configuration is not UTF-8") from error

        directives: dict[str, str] = {}
        for line in text.splitlines():
            if not line:
                continue
            name, separator, value = line.partition(" ")
            if name not in _DIRECTIVE_ORDER:
                raise BootManifestError(f"unknown extlinux directive: {name}")
            if not separator or not value or value != value.strip():
                raise BootManifestError(f"invalid {name} directive")
            if name in directives:
                raise BootManifestError(f"duplicate {name} directive")
            directives[name] = value

        for name in _DIRECTIVE_ORDER:
            if name not in directives:
                raise BootManifestError(f"missing {name} directive")
        if directives["default"] != directives["label"]:
            raise BootManifestError("default and label differ")
        if _SAFE_LABEL.fullmatch(directives["label"]) is None:
            raise BootManifestError("unsafe extlinux label")
        for name in _DIRECTIVE_TO_ARTIFACT:
            if _SAFE_PATH.fullmatch(directives[name]) is None:
                raise BootManifestError(f"unsafe {name} path")

        return cls(**directives)

    @property
    def artifact_paths(self) -> dict[str, str]:
        return {
            artifact: getattr(self, directive)
            for directive, artifact in _DIRECTIVE_TO_ARTIFACT.items()
        }

    def canonical_bytes(self) -> bytes:
        return "".join(
            f"{name} {getattr(self, name)}\n" for name in _DIRECTIVE_ORDER
        ).encode()

    def validate_against_plan(self, plan: Any) -> None:
        try:
            plan.validate()
        except (AttributeError, TypeError, ValueError) as error:
            raise BootManifestError(f"invalid debug plan: {error}") from error
        if self.label != f"asterinas-{plan.plan_sha256[:12]}":
            raise BootManifestError("extlinux label does not identify the debug plan")
        if self.append != plan.bootargs:
            raise BootManifestError("extlinux append does not match debug plan bootargs")

        identities = {identity.name: identity for identity in plan.artifacts}
        if any(name not in identities for name in ARTIFACT_ORDER):
            raise BootManifestError("debug plan lacks a Megrez boot artifact")
        for name, path in self.artifact_paths.items():
            identity = identities[name]
            if identity.sha256[:12] not in Path(path).name:
                raise BootManifestError(
                    f"{name}: immutable SHA-256 prefix is absent from the basename"
                )

    def validate_staged_directory(self, root: Path, plan: Any) -> tuple[str, ...]:
        try:
            root_metadata = root.lstat()
        except OSError as error:
            raise BootManifestError(f"staged directory is unavailable: {error}") from error
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(
            root_metadata.st_mode
        ):
            raise BootManifestError("staged directory is not a non-symlink directory")

        paths: dict[str, Path] = {}
        metadata: dict[str, os.stat_result] = {}
        for name in ARTIFACT_ORDER:
            path = root / self.artifact_paths[name].removeprefix("/")
            paths[name] = path
            try:
                current = path.lstat()
            except FileNotFoundError as error:
                raise BootManifestError(f"{name}: staged file is missing") from error
            except OSError as error:
                raise BootManifestError(f"{name}: staged file is unavailable") from error
            if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
                raise BootManifestError(
                    f"{name}: staged file is not a regular non-symlink file"
                )
            metadata[name] = current

        self.validate_against_plan(plan)
        identities = {identity.name: identity for identity in plan.artifacts}
        for name in ARTIFACT_ORDER:
            identity = identities[name]
            current = metadata[name]
            if current.st_size != identity.size:
                raise BootManifestError(f"{name}: staged size mismatch")
            payload = _read_regular_unchanged(paths[name], current, identity.size, name)
            if hashlib.sha256(payload).hexdigest() != identity.sha256:
                raise BootManifestError(f"{name}: staged SHA-256 mismatch")
            if f"{zlib.crc32(payload):08x}" != identity.crc32:
                raise BootManifestError(f"{name}: staged CRC32 mismatch")
        return ARTIFACT_ORDER


def _read_regular_unchanged(
    path: Path, before: os.stat_result, expected_size: int, name: str
) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BootManifestError(f"{name}: staged file changed while opening") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != expected_size
        ):
            raise BootManifestError(f"{name}: staged file changed while opening")
        payload = bytearray()
        while len(payload) <= expected_size:
            chunk = os.read(descriptor, expected_size + 1 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(payload) != expected_size
            or after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
        ):
            raise BootManifestError(f"{name}: staged file changed while reading")
        return bytes(payload)
    finally:
        os.close(descriptor)
