#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Strict identity validation for a frozen Debian RISC-V root filesystem."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit


ROOT_LABEL = "ASTER_DEBIANROOT"
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
_DEBIAN_RELEASE_RE = re.compile(r"\A13\.(?:0|[1-9][0-9]*)\Z")
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

    with path.open("rb") as input_file:
        return _sha256_stream(input_file)


def load_manifest(path: Path) -> RootfsManifest:
    """Loads a rootfs manifest after strict structural validation."""

    try:
        document = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ContractError("manifest must be UTF-8") from error
    try:
        raw = json.loads(
            document,
            object_pairs_hook=_unique_json_object,
        )
    except json.JSONDecodeError as error:
        raise ContractError(
            f"invalid manifest JSON at line {error.lineno}, "
            f"column {error.colno}: {error.msg}"
        ) from error
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

    rows, _ = _load_packages_lock(path)
    return rows


def _parse_packages_lock_text(value: str) -> tuple[PackageLockRow, ...]:
    rows: list[PackageLockRow] = []
    for line_number, line in enumerate(value.splitlines(), start=1):
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
    if _DEBIAN_RELEASE_RE.fullmatch(manifest.debian_release) is None:
        raise ContractError(
            f"Debian release is not a signed Debian 13 point release: "
            f"{manifest.debian_release!r}"
        )
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

    rows, actual_lock_sha256 = _load_packages_lock(packages_lock)
    if rows != tuple(sorted(rows)) or len(rows) != len(set(rows)):
        raise ContractError("packages.lock rows must be sorted and unique")
    package_identities = tuple((name, architecture) for name, architecture, _ in rows)
    if len(package_identities) != len(set(package_identities)):
        raise ContractError("packages.lock package identities must be unique")

    downloaded_packages = manifest.downloaded_packages
    if downloaded_packages != tuple(sorted(downloaded_packages)):
        raise ContractError("downloaded package identities must be sorted")
    downloaded_identities = tuple(
        (name, architecture) for name, architecture, _, _ in downloaded_packages
    )
    if len(downloaded_identities) != len(set(downloaded_identities)):
        raise ContractError("downloaded package identities must be unique")

    locked_rows = set(rows)
    for name, architecture, version, _ in downloaded_packages:
        if (name, architecture, version) not in locked_rows:
            raise ContractError(
                f"downloaded package {name}/{architecture}/{version} "
                "does not match packages.lock"
            )

    downloaded_names = {name for name, _, _, _ in downloaded_packages}
    missing_install_packages = set(INSTALL_PACKAGES) - downloaded_names
    if missing_install_packages:
        raise ContractError(
            "downloaded package identities are missing explicit install packages: "
            f"{sorted(missing_install_packages)}"
        )

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

    if not hmac.compare_digest(
        manifest.packages_lock_sha256,
        actual_lock_sha256,
    ):
        raise ContractError("package-lock SHA-256 does not match packages.lock")

    with image.open("rb") as image_file:
        actual_image_size_bytes = os.fstat(image_file.fileno()).st_size
        if actual_image_size_bytes != _ROOT_IMAGE_SIZE_BYTES:
            raise ContractError(
                f"image size is {actual_image_size_bytes}, expected "
                f"{_ROOT_IMAGE_SIZE_BYTES}"
            )
        actual_image_sha256 = _sha256_stream(image_file)
    if not hmac.compare_digest(
        manifest.root_image_sha256,
        actual_image_sha256,
    ):
        raise ContractError("image SHA-256 does not match root_image_sha256")

    return manifest


def write_manifest(
    *,
    output: Path,
    image: Path,
    packages_lock: Path,
    inrelease: Path,
    package_checksums: Path,
    mirror_url: str,
    suite: str,
    debian_release: str,
    build_timestamp: str,
    tool_versions: Sequence[str],
) -> None:
    """Validates build inputs and atomically writes a canonical manifest."""

    _require_safe_output_path(output)
    _require_exact(suite, _SUITE, "suite")
    _require_https(mirror_url, "mirror_url")
    if _DEBIAN_RELEASE_RE.fullmatch(debian_release) is None:
        raise ContractError("debian_release must be a signed Debian 13 point release")
    _require_build_timestamp(build_timestamp)

    lock_rows = parse_packages_lock(packages_lock)
    downloaded_packages = _load_package_checksums(package_checksums)
    parsed_tool_versions = _parse_tool_versions(tool_versions)
    gate_versions = _gate_versions(lock_rows)

    manifest = {
        "architecture": _ARCHITECTURE,
        "build_timestamp": build_timestamp,
        "debian_release": debian_release,
        "downloaded_packages": [
            {
                "architecture": architecture,
                "name": name,
                "sha256": sha256,
                "version": version,
            }
            for name, architecture, version, sha256 in downloaded_packages
        ],
        "filesystem": {
            "block_size_bytes": _FILESYSTEM_BLOCK_SIZE_BYTES,
            "label": ROOT_LABEL,
            "size_bytes": _ROOT_IMAGE_SIZE_BYTES,
            "type": _FILESYSTEM_TYPE,
            "uuid": ROOT_UUID,
        },
        "gate_packages": gate_versions,
        "mirror_url": mirror_url,
        "packages_lock_sha256": sha256_file(packages_lock),
        "root_image_sha256": sha256_file(image),
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "signed_metadata": {
            "sha256": sha256_file(inrelease),
            "url": f"{mirror_url.rstrip('/')}/dists/{suite}/InRelease",
        },
        "suite": suite,
        "tool_versions": parsed_tool_versions,
    }
    serialized = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    _write_validated_manifest_atomically(
        output,
        serialized,
        image,
        packages_lock,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    """Runs the explicit manifest-writer command-line interface."""

    raw_arguments = list(sys.argv[1:] if arguments is None else arguments)
    _reject_duplicate_cli_options(raw_arguments)
    parser = _argument_parser()
    namespace = parser.parse_args(raw_arguments)

    try:
        write_manifest(
            output=namespace.output,
            image=namespace.image,
            packages_lock=namespace.packages_lock,
            inrelease=namespace.inrelease,
            package_checksums=namespace.package_checksums,
            mirror_url=namespace.mirror,
            suite=namespace.suite,
            debian_release=namespace.debian_release,
            build_timestamp=namespace.build_timestamp,
            tool_versions=namespace.tool_version,
        )
    except (ContractError, OSError) as error:
        parser.error(str(error))
    return 0


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{path} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ContractError(f"{path} keys must be strings")
    return value


def _sha256_stream(input_file: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(
        lambda: input_file.read(_HASH_CHUNK_SIZE_BYTES),
        b"",
    ):
        digest.update(chunk)
    return digest.hexdigest()


def _load_packages_lock(path: Path) -> tuple[tuple[PackageLockRow, ...], str]:
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    with path.open("rb") as input_file:
        for chunk in iter(
            lambda: input_file.read(_HASH_CHUNK_SIZE_BYTES),
            b"",
        ):
            chunks.append(chunk)
            digest.update(chunk)

    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError("packages.lock must be UTF-8") from error
    return _parse_packages_lock_text(text), digest.hexdigest()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractError(f"duplicate JSON key: {key!r}")
        value[key] = item
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
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError as error:
        raise ContractError(f"{path} must be an HTTPS URL: {error}") from error
    if parsed.scheme != "https" or not parsed.netloc or hostname is None:
        raise ContractError(f"{path} must be an HTTPS URL")


def _require_exact(actual: object, expected: object, path: str) -> None:
    if actual != expected:
        raise ContractError(f"unexpected {path}: {actual!r}; expected {expected!r}")


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="contract")
    subparsers = parser.add_subparsers(dest="command", required=True)
    writer = subparsers.add_parser("write-manifest")
    writer.add_argument("--output", required=True, type=Path)
    writer.add_argument("--image", required=True, type=Path)
    writer.add_argument("--packages-lock", required=True, type=Path)
    writer.add_argument("--inrelease", required=True, type=Path)
    writer.add_argument("--package-checksums", required=True, type=Path)
    writer.add_argument("--mirror", required=True)
    writer.add_argument("--suite", required=True)
    writer.add_argument("--debian-release", required=True)
    writer.add_argument("--build-timestamp", required=True)
    writer.add_argument("--tool-version", action="append", required=True)
    return parser


def _reject_duplicate_cli_options(arguments: Sequence[str]) -> None:
    repeatable = {"--tool-version"}
    seen: set[str] = set()
    for argument in arguments:
        option = argument.split("=", maxsplit=1)[0]
        if not option.startswith("--") or option in repeatable:
            continue
        if option in seen:
            raise ContractError(f"duplicate command-line option: {option}")
        seen.add(option)


def _require_build_timestamp(value: str) -> None:
    if (
        re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value)
        is None
    ):
        raise ContractError("build_timestamp must be canonical UTC RFC 3339")
    try:
        datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise ContractError("build_timestamp must be canonical UTC RFC 3339") from error


def _load_package_checksums(path: Path) -> tuple[DownloadedPackageIdentity, ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ContractError("package-checksums must be UTF-8") from error

    rows: list[DownloadedPackageIdentity] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        fields = line.split("\t")
        if len(fields) != 4 or any(not field for field in fields):
            raise ContractError(
                f"package-checksums line {line_number} must contain four "
                "non-empty tab-separated fields"
            )
        sha256 = _sha256(fields[3], f"package-checksums line {line_number} SHA-256")
        rows.append((fields[0], fields[1], fields[2], sha256))

    identities = tuple(rows)
    if not identities:
        raise ContractError("package-checksums must contain at least one row")
    if identities != tuple(sorted(identities)) or len(identities) != len(
        set(identities)
    ):
        raise ContractError("package-checksums rows must be sorted and unique")
    package_identities = tuple(
        (name, architecture) for name, architecture, _, _ in identities
    )
    if len(package_identities) != len(set(package_identities)):
        raise ContractError("package-checksums package identities must be unique")
    return identities


def _parse_tool_versions(values: Sequence[str]) -> dict[str, str]:
    versions: dict[str, str] = {}
    for value in values:
        name, separator, version = value.partition("=")
        if not separator or not name or not version:
            raise ContractError("tool versions must use non-empty NAME=VERSION values")
        if name in versions:
            raise ContractError(f"duplicate tool version: {name}")
        versions[name] = version
    return dict(sorted(versions.items()))


def _gate_versions(rows: Sequence[PackageLockRow]) -> dict[str, str]:
    versions: dict[str, str] = {}
    for package_name in GATE_IDENTITY_PACKAGES:
        matches = [
            version
            for name, architecture, version in rows
            if name == package_name and architecture == _ARCHITECTURE
        ]
        if len(matches) != 1:
            raise ContractError(
                f"gate package {package_name} must have exactly one {_ARCHITECTURE} lock row"
            )
        versions[package_name] = matches[0]
    return versions


def _require_safe_output_path(path: Path) -> None:
    absolute = path.absolute()
    for component in (absolute, *absolute.parents):
        if component.is_symlink():
            raise ContractError(f"manifest output path contains a symlink: {component}")
    if path.exists() and not path.is_file():
        raise ContractError("manifest output target must be a regular file")
    if not path.parent.is_dir():
        raise ContractError("manifest output parent must be an existing directory")


def _write_validated_manifest_atomically(
    output: Path,
    serialized: str,
    image: Path,
    packages_lock: Path,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output_file:
            output_file.write(serialized)
            output_file.flush()
            os.fsync(output_file.fileno())

        manifest = load_manifest(temporary_path)
        validate_frozen_root(image, manifest, packages_lock)
        os.replace(temporary_path, output)
        directory_descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
