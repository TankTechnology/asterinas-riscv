#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Materialize audited development overlays on a frozen Debian ext2 rootfs."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from tools.riscv.debian.rootfs.contract import (
    load_manifest,
    load_package_checksums,
    validate_frozen_root,
)


_SPEC_KEYS = {"schema_version", "profile", "files"}
_FILE_KEYS = {"source", "destination", "mode"}
_MODE_RE = re.compile(r"\A0[0-7]{3}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_DESTINATION_RE = re.compile(r"\A/[A-Za-z0-9._+@%:,=/~-]+\Z")
_HASH_CHUNK_SIZE = 1024 * 1024
# Linux documents the shared ext2/ext4 superblock at byte 1024, with `s_wtime`
# at offset 0x30 and `s_magic` at 0x38. See
# Documentation/filesystems/ext4/super.rst in the Linux kernel source.
_EXT_SUPERBLOCK_OFFSET = 1024
_EXT_WRITE_TIME_OFFSET = _EXT_SUPERBLOCK_OFFSET + 48
_EXT_MAGIC_OFFSET = _EXT_SUPERBLOCK_OFFSET + 56
_EXT_MAGIC = b"\x53\xef"


class OverlayError(ValueError):
    """A development overlay or its materialized image is unsafe or invalid."""


@dataclass(frozen=True)
class OverlayFile:
    """One immutable regular-file replacement in a development overlay."""

    source_name: str
    source: Path
    destination: str
    mode: int
    sha256: str


@dataclass(frozen=True)
class OverlaySpec:
    """An exact, content-addressed development overlay specification."""

    schema_version: int
    profile: str
    path: Path
    sha256: str
    files: tuple[OverlayFile, ...]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OverlayError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OverlayError(f"{name} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    missing = sorted(expected - set(value))
    unexpected = sorted(set(value) - expected)
    if missing:
        raise OverlayError(f"missing {name} fields: {missing}")
    if unexpected:
        raise OverlayError(f"unexpected {name} fields: {unexpected}")


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise OverlayError(f"{name} must be a non-empty string")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolve_source(spec_directory: Path, source_name: str) -> Path:
    relative = Path(source_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise OverlayError(
            f"overlay source must remain beneath the specification directory: {source_name}"
        )
    if any(part in {"", "."} for part in relative.parts):
        raise OverlayError("source must be a canonical relative path")
    candidate = spec_directory / relative
    current = candidate
    while current != spec_directory:
        if current.is_symlink():
            raise OverlayError(f"overlay source must not use symlinks: {source_name}")
        current = current.parent
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(spec_directory)
    except (FileNotFoundError, ValueError) as error:
        raise OverlayError(
            f"overlay source must remain beneath the specification directory: {source_name}"
        ) from error
    if not stat.S_ISREG(resolved.stat().st_mode):
        raise OverlayError(f"overlay source is not a regular file: {source_name}")
    return resolved


def _destination(value: Any) -> str:
    destination = _string(value, "files[].destination")
    parsed = PurePosixPath(destination)
    if (
        not parsed.is_absolute()
        or destination == "/"
        or str(parsed) != destination
        or any(part in {".", ".."} for part in parsed.parts)
    ):
        raise OverlayError(
            "destination must be a canonical absolute path without spaces"
        )
    if not _DESTINATION_RE.fullmatch(destination):
        raise OverlayError(
            "destination contains characters outside safe debugfs path characters"
        )
    return destination


def load_overlay_spec(
    path: Path, *, expected_profile: str | None = None
) -> OverlaySpec:
    """Load and fully validate one development-overlay JSON specification."""

    path = Path(path).resolve(strict=True)
    try:
        contents = path.read_bytes()
        raw = json.loads(contents, object_pairs_hook=_unique_object)
    except UnicodeDecodeError as error:
        raise OverlayError("overlay specification must be UTF-8") from error
    except json.JSONDecodeError as error:
        raise OverlayError(
            f"invalid overlay JSON at line {error.lineno}, column {error.colno}"
        ) from error
    document = _mapping(raw, "overlay")
    _exact_keys(document, _SPEC_KEYS, "overlay")
    if document["schema_version"] != 1:
        raise OverlayError("unsupported overlay schema version")
    profile = _string(document["profile"], "profile")
    if expected_profile is not None and profile != expected_profile:
        raise OverlayError(
            f"overlay profile does not match base profile: {profile} != {expected_profile}"
        )
    raw_files = document["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise OverlayError("overlay must contain at least one file")

    files: list[OverlayFile] = []
    destinations: set[str] = set()
    for index, raw_file in enumerate(raw_files):
        item = _mapping(raw_file, f"files[{index}]")
        _exact_keys(item, _FILE_KEYS, f"files[{index}]")
        source_name = _string(item["source"], f"files[{index}].source")
        source = _resolve_source(path.parent, source_name)
        destination = _destination(item["destination"])
        if destination in destinations:
            raise OverlayError(f"duplicate destination: {destination}")
        destinations.add(destination)
        mode_text = _string(item["mode"], f"files[{index}].mode")
        if not _MODE_RE.fullmatch(mode_text):
            raise OverlayError("file mode must be 0 followed by three octal digits")
        files.append(
            OverlayFile(
                source_name=source_name,
                source=source,
                destination=destination,
                mode=int(mode_text, 8),
                sha256=_sha256_file(source),
            )
        )
    return OverlaySpec(
        1, profile, path, hashlib.sha256(contents).hexdigest(), tuple(files)
    )


def _run_debugfs(image: Path, command: str, *, writable: bool = False) -> str:
    arguments = ["debugfs"]
    if writable:
        arguments.append("-w")
    arguments.extend(("-R", command, str(image)))
    result = subprocess.run(arguments, capture_output=True, text=True)
    if result.returncode != 0:
        raise OverlayError(f"debugfs failed for {command!r}: {result.stderr.strip()}")
    return result.stdout


def _require_existing_regular_file(image: Path, destination: str) -> None:
    output = _run_debugfs(image, f"stat {destination}")
    if "Inode:" not in output:
        raise OverlayError(f"overlay destination does not exist: {destination}")
    if "Type: regular" not in output:
        raise OverlayError(f"overlay destination is not a regular file: {destination}")


def _apply_file(image: Path, entry: OverlayFile, scratch: Path, index: int) -> None:
    _require_existing_regular_file(image, entry.destination)
    staged_source = scratch / f"source-{index:04d}"
    shutil.copyfile(entry.source, staged_source)
    commands = (
        f"rm {entry.destination}",
        f"write {staged_source} {entry.destination}",
        f"set_inode_field {entry.destination} mode {stat.S_IFREG | entry.mode}",
        f"set_inode_field {entry.destination} uid 0",
        f"set_inode_field {entry.destination} gid 0",
        f"set_inode_field {entry.destination} atime 0",
        f"set_inode_field {entry.destination} ctime 0",
        f"set_inode_field {entry.destination} mtime 0",
        f"set_inode_field {entry.destination} crtime 0",
    )
    for command in commands:
        _run_debugfs(image, command, writable=True)

    dumped = scratch / f"dump-{index:04d}"
    _run_debugfs(image, f"dump {entry.destination} {dumped}")
    if _sha256_file(dumped) != entry.sha256:
        raise OverlayError(f"overlay byte verification failed: {entry.destination}")
    stat_output = _run_debugfs(image, f"stat {entry.destination}")
    if "Type: regular" not in stat_output:
        raise OverlayError(
            f"overlay file-type verification failed: {entry.destination}"
        )
    match = re.search(r"\bMode:\s+0*([0-7]{3,4})\b", stat_output)
    if match is None or int(match.group(1), 8) & 0o7777 != entry.mode:
        raise OverlayError(f"overlay mode verification failed: {entry.destination}")


def _restore_ext_write_time(base_image: Path, derived_image: Path) -> None:
    """Restore ext's superblock write time after deterministic debugfs edits."""

    with base_image.open("rb") as base, derived_image.open("r+b") as derived:
        base.seek(_EXT_MAGIC_OFFSET)
        derived.seek(_EXT_MAGIC_OFFSET)
        if base.read(len(_EXT_MAGIC)) != _EXT_MAGIC:
            raise OverlayError("base image does not contain an ext superblock")
        if derived.read(len(_EXT_MAGIC)) != _EXT_MAGIC:
            raise OverlayError("derived image does not contain an ext superblock")
        base.seek(_EXT_WRITE_TIME_OFFSET)
        write_time = base.read(4)
        if len(write_time) != 4:
            raise OverlayError("base ext superblock is truncated")
        derived.seek(_EXT_WRITE_TIME_OFFSET)
        if derived.write(write_time) != len(write_time):
            raise OverlayError("failed to restore the ext superblock write time")
        derived.flush()
        os.fsync(derived.fileno())


def materialize_image(base_image: Path, output_image: Path, spec: OverlaySpec) -> str:
    """Publish a verified derived ext2 image while preserving the frozen base."""

    base_input = Path(base_image).absolute()
    if base_input.is_symlink() or not stat.S_ISREG(base_input.stat().st_mode):
        raise OverlayError("base image must be a non-symlink regular file")
    base_image = base_input.resolve(strict=True)
    output_image = Path(output_image).absolute()
    if output_image == base_image or (
        output_image.exists() and os.path.samefile(base_image, output_image)
    ):
        raise OverlayError("output image must differ from the base image")
    output_image.parent.mkdir(parents=True, exist_ok=True)
    if output_image.is_symlink():
        raise OverlayError("output image must not be a symlink")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_image.name}.", suffix=".tmp", dir=output_image.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    try:
        subprocess.run(
            [
                "cp",
                "--reflink=auto",
                "--sparse=always",
                "--",
                str(base_image),
                str(temporary),
            ],
            check=True,
        )
        with tempfile.TemporaryDirectory(prefix="asterinas-dev-overlay-") as directory:
            scratch = Path(directory)
            for index, entry in enumerate(spec.files):
                _apply_file(temporary, entry, scratch, index)
        _restore_ext_write_time(base_image, temporary)
        derived_sha256 = _sha256_file(temporary)
        os.replace(temporary, output_image)
        return derived_sha256
    except (OSError, subprocess.SubprocessError) as error:
        raise OverlayError(
            f"failed to materialize development overlay: {error}"
        ) from error
    finally:
        temporary.unlink(missing_ok=True)


def build_derived_documents(
    base_document: Mapping[str, Any],
    *,
    base_manifest_sha256: str,
    derived_image_sha256: str,
    spec: OverlaySpec,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build gate-compatible and explicit provenance documents for a derivative."""

    base_root_sha256 = base_document.get("root_image_sha256")
    if not isinstance(base_root_sha256, str) or not _SHA256_RE.fullmatch(
        base_root_sha256
    ):
        raise OverlayError("base manifest root image SHA-256 is invalid")
    if base_document.get("profile") != spec.profile:
        raise OverlayError("overlay profile does not match base manifest")
    if not _SHA256_RE.fullmatch(base_manifest_sha256):
        raise OverlayError("base manifest SHA-256 is invalid")
    if not _SHA256_RE.fullmatch(derived_image_sha256):
        raise OverlayError("derived image SHA-256 is invalid")

    files = [
        {
            "source": entry.source_name,
            "destination": entry.destination,
            "mode": f"0{entry.mode:03o}",
            "sha256": entry.sha256,
        }
        for entry in spec.files
    ]
    derivation = {
        "schema_version": 1,
        "profile": spec.profile,
        "base_manifest_sha256": base_manifest_sha256,
        "base_root_image_sha256": base_root_sha256,
        "overlay_spec_sha256": spec.sha256,
        "files": files,
    }
    derivation_sha256 = _canonical_sha256(derivation)
    companion = dict(derivation)
    companion["derivation_sha256"] = derivation_sha256
    companion["derived_root_image_sha256"] = derived_image_sha256

    derived = copy.deepcopy(dict(base_document))
    derived["root_image_sha256"] = derived_image_sha256
    versions = dict(_mapping(derived.get("tool_versions"), "tool_versions"))
    versions["asterinas-dev-overlay"] = derivation_sha256
    derived["tool_versions"] = dict(sorted(versions.items()))
    return derived, companion


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OverlayError(f"invalid base manifest JSON: {error}") from error
    return dict(_mapping(value, "base manifest"))


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _publish_directory(staged: Path, output: Path) -> None:
    backup = output.with_name(f".{output.name}.previous")
    if backup.exists():
        raise OverlayError(f"stale overlay publication backup exists: {backup}")
    replaced = False
    try:
        if output.exists():
            if output.is_symlink() or not output.is_dir():
                raise OverlayError("overlay output must be a directory or absent")
            os.replace(output, backup)
            replaced = True
        os.replace(staged, output)
    except BaseException:
        if replaced and backup.exists() and not output.exists():
            os.replace(backup, output)
        raise
    if replaced:
        shutil.rmtree(backup)


def materialize_rootfs(
    base_directory: Path, spec_path: Path, output_directory: Path
) -> dict[str, Any]:
    """Create a drop-in rootfs artifact directory without network or package work."""

    started = time.monotonic()
    base_directory = Path(base_directory).resolve(strict=True)
    output_directory = Path(output_directory).absolute().resolve(strict=False)
    if (
        output_directory == base_directory
        or output_directory in base_directory.parents
        or base_directory in output_directory.parents
    ):
        raise OverlayError("base and output rootfs directories must not overlap")
    base_image = base_directory / "debian-root.ext2"
    base_manifest_path = base_directory / "rootfs-manifest.json"
    packages_lock = base_directory / "packages.lock"
    package_checksums = base_directory / "source-metadata/package-checksums"
    manifest = load_manifest(base_manifest_path)
    validate_frozen_root(base_image, manifest, packages_lock)
    checksums = load_package_checksums(
        package_checksums, schema_version=manifest.schema_version
    )
    if checksums != manifest.downloaded_packages:
        raise OverlayError("base package-checksums do not match the base manifest")
    spec = load_overlay_spec(spec_path, expected_profile=manifest.profile)
    base_document = _read_json(base_manifest_path)

    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}.",
            suffix=".tmp",
            dir=output_directory.parent,
        )
    )
    try:
        derived_image = staged / "debian-root.ext2"
        derived_sha256 = materialize_image(base_image, derived_image, spec)
        shutil.copy2(packages_lock, staged / "packages.lock")
        shutil.copytree(base_directory / "source-metadata", staged / "source-metadata")
        derived, companion = build_derived_documents(
            base_document,
            base_manifest_sha256=_sha256_file(base_manifest_path),
            derived_image_sha256=derived_sha256,
            spec=spec,
        )
        _write_json(staged / "rootfs-manifest.json", derived)
        _write_json(staged / "dev-overlay-manifest.json", companion)
        validate_frozen_root(
            derived_image,
            load_manifest(staged / "rootfs-manifest.json"),
            staged / "packages.lock",
        )
        _publish_directory(staged, output_directory)
    finally:
        if staged.exists():
            shutil.rmtree(staged)

    return {
        "base_directory": str(base_directory),
        "output_directory": str(output_directory),
        "profile": spec.profile,
        "files": len(spec.files),
        "root_image_sha256": derived_sha256,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    materialize = subparsers.add_parser(
        "materialize", help="materialize a development rootfs"
    )
    materialize.add_argument("--base-dir", required=True, type=Path)
    materialize.add_argument("--spec", required=True, type=Path)
    materialize.add_argument("--output-dir", required=True, type=Path)
    values = parser.parse_args(arguments)
    try:
        result = materialize_rootfs(values.base_dir, values.spec, values.output_dir)
    except (OSError, OverlayError, subprocess.SubprocessError, ValueError) as error:
        print(f"dev-overlay: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
