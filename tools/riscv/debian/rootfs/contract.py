#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Strict identity validation for a frozen Debian RISC-V root filesystem."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT_LABEL = "ASTER_DEBIAN_ROOT"
ROOT_UUID = "7b7ad749-77d0-4e59-89e4-e117244a70aa"
INSTALL_PACKAGES = (
    "bash",
    "ca-certificates",
    "coreutils",
    "procps",
    "util-linux",
)
GATE_IDENTITY_PACKAGES = (
    "base-files",
    "libc6",
    "bash",
    "coreutils",
    "util-linux",
)

SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")

_MANIFEST_SCHEMA_VERSION = 1
_SUITE = "trixie"
_DEBIAN_RELEASE = "13"
_ARCHITECTURE = "riscv64"
_FILESYSTEM_TYPE = "ext2"
_ROOT_IMAGE_SIZE_BYTES = 1024 * 1024 * 1024
_FILESYSTEM_BLOCK_SIZE_BYTES = 4096
_HASH_CHUNK_SIZE_BYTES = 1024 * 1024

_MANIFEST_KEYS = {
    "schema_version",
    "suite",
    "debian_release",
    "mirror_url",
    "architecture",
    "signed_metadata",
    "packages_lock_sha256",
    "downloaded_packages",
    "filesystem",
    "tool_versions",
    "build_timestamp",
    "root_image_sha256",
    "gate_packages",
}
_SIGNED_METADATA_KEYS = {"url", "sha256"}
_DOWNLOADED_PACKAGE_KEYS = {"name", "architecture", "version", "sha256"}
_FILESYSTEM_KEYS = {
    "type",
    "label",
    "uuid",
    "size_bytes",
    "block_size_bytes",
}

PackageLockRow = tuple[str, str, str]
DownloadedPackageIdentity = tuple[str, str, str, str]


class ContractError(ValueError):
    """A rootfs identity document violates the frozen-build contract."""


@dataclass(frozen=True)
class FilesystemIdentity:
    """The immutable on-disk filesystem identity."""

    filesystem_type: str
    label: str
    uuid: str
    size_bytes: int
    block_size_bytes: int


@dataclass(frozen=True)
class RootfsManifest:
    """The immutable provenance and content identity of a Debian rootfs."""

    schema_version: int
    suite: str
    debian_release: str
    mirror_url: str
    architecture: str
    signed_metadata_url: str
    signed_metadata_sha256: str
    packages_lock_sha256: str
    downloaded_packages: tuple[DownloadedPackageIdentity, ...]
    filesystem: FilesystemIdentity
    tool_versions: tuple[tuple[str, str], ...]
    build_timestamp: str
    root_image_sha256: str
    gate_packages: tuple[tuple[str, str], ...]


def sha256_file(path: Path) -> str:
    """Returns the lowercase SHA-256 digest of a file using bounded reads."""

    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for chunk in iter(
            lambda: input_file.read(_HASH_CHUNK_SIZE_BYTES),
            b"",
        ):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> RootfsManifest:
    """Loads a rootfs manifest after strict structural validation."""

    raw = json.loads(path.read_text(encoding="utf-8"))
    manifest = _mapping(raw, "manifest")
    _exact_keys(manifest, _MANIFEST_KEYS, "manifest")

    signed_metadata = _mapping(
        manifest["signed_metadata"],
        "signed_metadata",
    )
    _exact_keys(
        signed_metadata,
        _SIGNED_METADATA_KEYS,
        "signed_metadata",
    )

    filesystem = _mapping(manifest["filesystem"], "filesystem")
    _exact_keys(filesystem, _FILESYSTEM_KEYS, "filesystem")

    downloaded_packages = _downloaded_packages(manifest["downloaded_packages"])
    tool_versions = _string_mapping(
        manifest["tool_versions"],
        "tool_versions",
    )

    gate_packages = _mapping(manifest["gate_packages"], "gate_packages")
    _exact_keys(
        gate_packages,
        set(GATE_IDENTITY_PACKAGES),
        "gate_packages",
    )

    return RootfsManifest(
        schema_version=_integer(manifest["schema_version"], "schema_version"),
        suite=_string(manifest["suite"], "suite"),
        debian_release=_string(
            manifest["debian_release"],
            "debian_release",
        ),
        mirror_url=_string(manifest["mirror_url"], "mirror_url"),
        architecture=_string(manifest["architecture"], "architecture"),
        signed_metadata_url=_string(
            signed_metadata["url"],
            "signed_metadata.url",
        ),
        signed_metadata_sha256=_sha256(
            signed_metadata["sha256"],
            "signed_metadata.sha256",
        ),
        packages_lock_sha256=_sha256(
            manifest["packages_lock_sha256"],
            "packages_lock_sha256",
        ),
        downloaded_packages=downloaded_packages,
        filesystem=FilesystemIdentity(
            filesystem_type=_string(filesystem["type"], "filesystem.type"),
            label=_string(filesystem["label"], "filesystem.label"),
            uuid=_string(filesystem["uuid"], "filesystem.uuid"),
            size_bytes=_integer(
                filesystem["size_bytes"],
                "filesystem.size_bytes",
            ),
            block_size_bytes=_integer(
                filesystem["block_size_bytes"],
                "filesystem.block_size_bytes",
            ),
        ),
        tool_versions=tool_versions,
        build_timestamp=_string(
            manifest["build_timestamp"],
            "build_timestamp",
        ),
        root_image_sha256=_sha256(
            manifest["root_image_sha256"],
            "root_image_sha256",
        ),
        gate_packages=tuple(
            (name, _string(gate_packages[name], f"gate_packages.{name}"))
            for name in GATE_IDENTITY_PACKAGES
        ),
    )


def parse_packages_lock(path: Path) -> tuple[PackageLockRow, ...]:
    """Parses immutable name, architecture, and version lock rows."""

    rows: list[PackageLockRow] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        fields = line.split("\t")
        if len(fields) != 3 or any(not field for field in fields):
            raise ContractError(
                f"packages.lock line {line_number} must contain three "
                "non-empty tab-separated fields"
            )
        rows.append((fields[0], fields[1], fields[2]))

    if not rows:
        raise ContractError("packages.lock must contain at least one row")
    return tuple(rows)


def validate_frozen_root(
    image: Path,
    manifest: RootfsManifest,
    packages_lock: Path,
) -> RootfsManifest:
    """Validates a base image and its complete frozen-build identity."""

    _require_integer(manifest.schema_version, "schema_version")
    _require_exact(manifest.schema_version, _MANIFEST_SCHEMA_VERSION, "schema version")
    _require_exact(manifest.suite, _SUITE, "suite")
    _require_exact(manifest.debian_release, _DEBIAN_RELEASE, "Debian release")
    _require_exact(manifest.architecture, _ARCHITECTURE, "architecture")
    _require_https(manifest.mirror_url, "mirror_url")
    _require_https(manifest.signed_metadata_url, "signed_metadata.url")

    filesystem = manifest.filesystem
    _require_integer(filesystem.size_bytes, "filesystem.size_bytes")
    _require_integer(filesystem.block_size_bytes, "filesystem.block_size_bytes")
    _require_exact(filesystem.filesystem_type, _FILESYSTEM_TYPE, "filesystem type")
    _require_exact(filesystem.label, ROOT_LABEL, "filesystem label")
    _require_exact(filesystem.uuid, ROOT_UUID, "filesystem UUID")
    _require_exact(
        filesystem.size_bytes,
        _ROOT_IMAGE_SIZE_BYTES,
        "filesystem size",
    )
    _require_exact(
        filesystem.block_size_bytes,
        _FILESYSTEM_BLOCK_SIZE_BYTES,
        "filesystem block size",
    )

    rows = parse_packages_lock(packages_lock)
    if rows != tuple(sorted(rows)) or len(rows) != len(set(rows)):
        raise ContractError("packages.lock rows must be sorted and unique")

    gate_versions = dict(manifest.gate_packages)
    for package_name in GATE_IDENTITY_PACKAGES:
        locked_versions = [
            version
            for name, architecture, version in rows
            if name == package_name and architecture == _ARCHITECTURE
        ]
        if len(locked_versions) != 1:
            raise ContractError(
                f"gate package {package_name} must have exactly one "
                f"{_ARCHITECTURE} lock row"
            )
        if gate_versions.get(package_name) != locked_versions[0]:
            raise ContractError(
                f"gate package {package_name} version does not match packages.lock"
            )

    actual_lock_sha256 = sha256_file(packages_lock)
    if not hmac.compare_digest(
        manifest.packages_lock_sha256,
        actual_lock_sha256,
    ):
        raise ContractError("package-lock SHA-256 does not match packages.lock")

    actual_image_size_bytes = image.stat().st_size
    if actual_image_size_bytes != _ROOT_IMAGE_SIZE_BYTES:
        raise ContractError(
            f"image size is {actual_image_size_bytes}, expected "
            f"{_ROOT_IMAGE_SIZE_BYTES}"
        )

    actual_image_sha256 = sha256_file(image)
    if not hmac.compare_digest(
        manifest.root_image_sha256,
        actual_image_sha256,
    ):
        raise ContractError("image SHA-256 does not match root_image_sha256")

    return manifest


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{path} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ContractError(f"{path} keys must be strings")
    return value


def _sequence(value: object, path: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractError(f"{path} must be an array")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    path: str,
) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ContractError(f"unknown {path} fields: {sorted(unknown)}")
    if missing:
        raise ContractError(f"missing {path} fields: {sorted(missing)}")


def _integer(value: object, path: str) -> int:
    _require_integer(value, path)
    return value


def _require_integer(value: object, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{path} must be an integer")


def _string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{path} must be a non-empty string")
    return value


def _sha256(value: object, path: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ContractError(f"{path} must be a lowercase SHA-256")
    return value


def _downloaded_packages(value: object) -> tuple[DownloadedPackageIdentity, ...]:
    packages = _sequence(value, "downloaded_packages")
    identities: list[DownloadedPackageIdentity] = []
    for index, package_value in enumerate(packages):
        path = f"downloaded_packages[{index}]"
        package = _mapping(package_value, path)
        _exact_keys(package, _DOWNLOADED_PACKAGE_KEYS, path)
        identities.append(
            (
                _string(package["name"], f"{path}.name"),
                _string(package["architecture"], f"{path}.architecture"),
                _string(package["version"], f"{path}.version"),
                _sha256(package["sha256"], f"{path}.sha256"),
            )
        )
    if not identities:
        raise ContractError("downloaded_packages must not be empty")
    return tuple(identities)


def _string_mapping(value: object, path: str) -> tuple[tuple[str, str], ...]:
    mapping = _mapping(value, path)
    if not mapping:
        raise ContractError(f"{path} must not be empty")
    return tuple(
        sorted(
            (
                _string(key, f"{path} key"),
                _string(item, f"{path}.{key}"),
            )
            for key, item in mapping.items()
        )
    )


def _require_https(value: str, path: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ContractError(f"{path} must be an HTTPS URL")


def _require_exact(actual: object, expected: object, path: str) -> None:
    if actual != expected:
        raise ContractError(f"unexpected {path}: {actual!r}; expected {expected!r}")
