#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Verify a pinned Debian source descriptor against a local manifest."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any


MAX_MANIFEST_BYTES = 64 * 1024
MAX_DSC_BYTES = 2 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class GateError(ValueError):
    """The source descriptor or its manifest is not admissible."""


@dataclass(frozen=True)
class SourcePackageResult:
    passed: bool
    reason: str
    package: str
    version: str
    archive_count: int


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GateError(f"{label} must be an object")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise GateError(f"{label} must be a non-empty string")
    return value


def _sha(value: object, label: str) -> str:
    digest = _string(value, label)
    if SHA256_RE.fullmatch(digest) is None:
        raise GateError(f"{label} must be a lowercase SHA-256 digest")
    return digest


def _bytes(path: Path, label: str, maximum: int) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise GateError(f"{label} is not a regular file")
        if path.stat().st_size > maximum:
            raise GateError(f"{label} exceeds its byte bound")
        return path.read_bytes()
    except OSError as error:
        raise GateError(f"cannot read {label}: {error}") from error


def _load(path: Path) -> dict[str, Any]:
    payload = _bytes(path, "manifest", MAX_MANIFEST_BYTES)
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GateError(f"invalid manifest JSON: {error}") from error
    root = _object(document, "manifest")
    if set(root) != {"schema_version", "package", "version", "format", "source"}:
        raise GateError("manifest has unexpected or missing fields")
    if root["schema_version"] != 1 or isinstance(root["schema_version"], bool):
        raise GateError("schema_version must be 1")
    _string(root["package"], "package")
    _string(root["version"], "version")
    _string(root["format"], "format")
    source = _object(root["source"], "source")
    if set(source) != {"suite", "base_url", "dsc", "archives"}:
        raise GateError("source has unexpected or missing fields")
    suite = _string(source["suite"], "source.suite")
    if suite != "trixie-security":
        raise GateError("source.suite must be trixie-security")
    base_url = _string(source["base_url"], "source.base_url")
    if not base_url.startswith("https://") or not base_url.endswith("/"):
        raise GateError("source.base_url must be an HTTPS directory URL")
    dsc = _object(source["dsc"], "source.dsc")
    if set(dsc) != {"filename", "sha256"}:
        raise GateError("source.dsc has unexpected or missing fields")
    filename = _string(dsc["filename"], "source.dsc.filename")
    if "/" in filename or "\\" in filename:
        raise GateError("source.dsc.filename must be a basename")
    _sha(dsc["sha256"], "source.dsc.sha256")
    rows = source["archives"]
    if not isinstance(rows, list) or len(rows) != 2:
        raise GateError("source.archives must contain two core archives")
    seen: set[str] = set()
    for index, row in enumerate(rows):
        item = _object(row, f"source.archives[{index}]")
        if set(item) != {"filename", "size_bytes", "sha256"}:
            raise GateError("source archive fields are invalid")
        name = _string(item["filename"], "source archive filename")
        if "/" in name or "\\" in name or name in seen:
            raise GateError("source archive filenames must be unique basenames")
        seen.add(name)
        size = item["size_bytes"]
        if type(size) is not int or size <= 0:
            raise GateError("source archive size_bytes must be positive")
        _sha(item["sha256"], "source archive sha256")
    return root


def _control_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        name, separator, value = line.partition(":")
        if separator and name in {"Format", "Source", "Version"}:
            fields[name] = value.strip()
    if set(fields) != {"Format", "Source", "Version"}:
        raise GateError("source descriptor control fields are incomplete")
    return fields


def _sha_rows(text: str) -> dict[str, tuple[str, int]]:
    rows: dict[str, tuple[str, int]] = {}
    in_section = False
    for line in text.splitlines():
        if line == "Checksums-Sha256:":
            in_section = True
            continue
        if in_section and line and not line.startswith(" "):
            break
        if not in_section or not line.startswith(" "):
            continue
        fields = line.split()
        if len(fields) != 3 or SHA256_RE.fullmatch(fields[0]) is None:
            continue
        try:
            size = int(fields[1])
        except ValueError:
            continue
        rows[fields[2]] = (fields[0], size)
    return rows


def run_gate(manifest_path: Path, dsc_path: Path) -> SourcePackageResult:
    """Verify the local descriptor is the exact manifest input."""

    manifest_path = manifest_path.resolve()
    dsc_path = dsc_path.resolve()
    manifest = _load(manifest_path)
    dsc_payload = _bytes(dsc_path, "source descriptor", MAX_DSC_BYTES)
    dsc_spec = manifest["source"]["dsc"]
    if dsc_path.name != dsc_spec["filename"]:
        raise GateError("descriptor filename mismatch")
    if hashlib.sha256(dsc_payload).hexdigest() != dsc_spec["sha256"]:
        raise GateError("descriptor hash mismatch")
    try:
        text = dsc_payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GateError("source descriptor is not UTF-8") from error
    fields = _control_fields(text)
    if fields["Format"] != manifest["format"]:
        raise GateError("descriptor format mismatch")
    if fields["Source"] != manifest["package"]:
        raise GateError("descriptor source mismatch")
    if fields["Version"] != manifest["version"]:
        raise GateError("descriptor version mismatch")
    rows = _sha_rows(text)
    for row in manifest["source"]["archives"]:
        actual = rows.get(row["filename"])
        expected = (row["sha256"], row["size_bytes"])
        if actual != expected:
            raise GateError(f"archive metadata mismatch: {row['filename']}")
    return SourcePackageResult(
        True,
        "source descriptor and core archive metadata verified",
        manifest["package"],
        manifest["version"],
        len(manifest["source"]["archives"]),
    )


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="firefox-source-package-gate")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dsc", type=Path, required=True)
    values = parser.parse_args(arguments)
    try:
        result = run_gate(values.manifest, values.dsc)
    except (GateError, OSError, ValueError) as error:
        print(f"FIREFOX_SOURCE_PACKAGE_GATE FAIL reason={' '.join(str(error).split())}")
        return 1
    print(
        "FIREFOX_SOURCE_PACKAGE_GATE PASS "
        f"package={result.package} version={result.version} archives={result.archive_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
