#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Admit one provenance-recorded Firefox ESR package.

This gate is deliberately separate from rootfs materialization.  It checks the
package bytes, Debian control identity, source-patch manifest, and patch bytes
before a future builder is allowed to hand a package to an overlay step.  It
never installs or extracts the package and never changes the compatibility
source tree.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any

try:
    from tools.riscv.debian.rootfs.firefox_compat import source_patch_gate
except ModuleNotFoundError:  # Direct invocation from the rootfs builder.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import source_patch_gate  # type: ignore[no-redef]


MAX_PROVENANCE_BYTES = 64 * 1024
MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024
MAX_PATCHES = 2
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class GateError(ValueError):
    """A package or provenance input fails the admission contract."""


@dataclass(frozen=True)
class PackageResult:
    passed: bool
    reason: str
    package: str
    version: str
    patch_count: int


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GateError(f"{label} must be an object")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise GateError(f"{label} must be a non-empty string")
    return value


def _sha(value: object, label: str) -> str:
    result = _string(value, label)
    if SHA256_RE.fullmatch(result) is None:
        raise GateError(f"{label} must be a lowercase SHA-256 digest")
    return result


def _safe_relative(value: object, label: str) -> str:
    path = _string(value, label)
    parsed = PurePosixPath(path)
    if (
        parsed.is_absolute()
        or not parsed.parts
        or any(part in ("", ".", "..") for part in parsed.parts)
        or "\\" in path
    ):
        raise GateError(f"{label} must be a safe relative path")
    return "/".join(parsed.parts)


def _read_regular(path: Path, label: str, maximum: int) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise GateError(f"{label} is not a regular file")
        if path.stat().st_size > maximum:
            raise GateError(f"{label} exceeds its byte bound")
        return path.read_bytes()
    except OSError as error:
        raise GateError(f"cannot read {label}: {error}") from error


def _under(root: Path, candidate: Path, label: str) -> Path:
    root_resolved = root.resolve()
    candidate_resolved = candidate.resolve()
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as error:
        raise GateError(f"{label} escapes compatibility root") from error
    return candidate_resolved


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_provenance(path: Path) -> dict[str, Any]:
    payload = _read_regular(path, "provenance", MAX_PROVENANCE_BYTES)
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GateError(f"invalid provenance JSON: {error}") from error
    root = _object(raw, "provenance")
    required = {"schema_version", "package", "source_manifest", "patches", "build"}
    if set(root) != required:
        raise GateError("provenance has unexpected or missing fields")
    if root["schema_version"] != 1 or isinstance(root["schema_version"], bool):
        raise GateError("provenance schema_version must be 1")
    package = _object(root["package"], "package")
    if set(package) != {"name", "architecture", "version", "sha256"}:
        raise GateError("package has unexpected or missing fields")
    for key in ("name", "architecture", "version"):
        _string(package[key], f"package.{key}")
    _sha(package["sha256"], "package.sha256")
    source = _object(root["source_manifest"], "source_manifest")
    if set(source) != {"path", "sha256"}:
        raise GateError("source_manifest has unexpected or missing fields")
    _safe_relative(source["path"], "source_manifest.path")
    _sha(source["sha256"], "source_manifest.sha256")
    rows = root["patches"]
    if not isinstance(rows, list) or len(rows) != MAX_PATCHES:
        raise GateError(f"patches must contain exactly {MAX_PATCHES} entries")
    names: set[str] = set()
    for index, row in enumerate(rows):
        item = _object(row, f"patches[{index}]")
        if set(item) != {"path", "sha256"}:
            raise GateError(f"patches[{index}] has unexpected or missing fields")
        name = _safe_relative(item["path"], f"patches[{index}].path")
        if name in names:
            raise GateError(f"duplicate patch path: {name}")
        names.add(name)
        _sha(item["sha256"], f"patches[{index}].sha256")
    build = _object(root["build"], "build")
    if set(build) != {"source_dsc_sha256", "builder_image_digest"}:
        raise GateError("build has unexpected or missing fields")
    _sha(build["source_dsc_sha256"], "build.source_dsc_sha256")
    image = _string(build["builder_image_digest"], "build.builder_image_digest")
    if not image.startswith("sha256:") or SHA256_RE.fullmatch(image[7:]) is None:
        raise GateError("build.builder_image_digest must be sha256:<digest>")
    return root


def _package_fields(path: Path) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["dpkg-deb", "--field", str(path), "Package", "Architecture", "Version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GateError(f"cannot inspect Debian package: {error}") from error
    if result.returncode != 0:
        raise GateError(
            "cannot inspect Debian package: "
            + " ".join(result.stderr.split())[:240]
        )
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        name, separator, value = line.partition(":")
        if separator:
            fields[name] = value.strip()
    if set(fields) != {"Package", "Architecture", "Version"}:
        raise GateError("Debian package control metadata is incomplete")
    return fields


def run_gate(package_path: Path, provenance_path: Path, compat_root: Path) -> PackageResult:
    """Verify a built package without extracting or installing it."""

    package_path = package_path.resolve()
    provenance_path = provenance_path.resolve()
    compat_root = compat_root.resolve()
    package_payload = _read_regular(package_path, "package", MAX_PACKAGE_BYTES)
    provenance = _load_provenance(provenance_path)
    package = provenance["package"]
    actual_hash = _hash(package_payload)
    if actual_hash != package["sha256"]:
        raise GateError("package hash mismatch")
    fields = _package_fields(package_path)
    for key, field in (
        ("name", "Package"),
        ("architecture", "Architecture"),
        ("version", "Version"),
    ):
        if fields[field] != package[key]:
            raise GateError(f"package metadata mismatch: {field}")

    source_spec = provenance["source_manifest"]
    source_path = _under(
        compat_root,
        compat_root / source_spec["path"],
        "source patch manifest",
    )
    source_payload = _read_regular(source_path, "source patch manifest", MAX_PROVENANCE_BYTES)
    if _hash(source_payload) != source_spec["sha256"]:
        raise GateError("source patch manifest hash mismatch")
    expected_patches = provenance["patches"]
    manifest = source_patch_gate.load_manifest(source_path)
    actual_patches = [
        {"path": spec.path, "sha256": spec.sha256} for spec in manifest.patches
    ]
    if actual_patches != expected_patches:
        raise GateError("provenance patch list disagrees with source manifest")
    for row in actual_patches:
        patch_path = _under(
            compat_root,
            compat_root / row["path"],
            f"patch {row['path']}",
        )
        patch_payload = _read_regular(patch_path, f"patch {row['path']}", 4 * 1024 * 1024)
        if _hash(patch_payload) != row["sha256"]:
            raise GateError(f"patch hash mismatch: {row['path']}")
    return PackageResult(True, "package and source patch provenance verified", fields["Package"], fields["Version"], len(actual_patches))


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="firefox-package-provenance-gate")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--compat-root", type=Path, required=True)
    values = parser.parse_args(arguments)
    try:
        result = run_gate(values.package, values.provenance, values.compat_root)
    except (GateError, OSError, ValueError) as error:
        print(f"FIREFOX_PACKAGE_PROVENANCE FAIL reason={' '.join(str(error).split())}")
        return 1
    print(
        "FIREFOX_PACKAGE_PROVENANCE PASS "
        f"package={result.package} version={result.version} patches={result.patch_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
