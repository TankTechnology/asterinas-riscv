#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Verify the bounded, source-only Firefox compatibility overlay.

The gate checks the patch and audited source hashes first, then copies only the
manifested source files into a disposable tree and runs GNU patch in dry-run
mode with zero fuzz. It never builds Firefox, modifies the supplied source
tree, starts QEMU, or installs an artifact.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from typing import Any


DEFAULT_MANIFEST = Path(__file__).with_name("source_patch_manifest.json")
MAX_MANIFEST_BYTES = 64 * 1024
MAX_PATCH_BYTES = 4 * 1024 * 1024
MAX_SOURCE_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 64 * 1024 * 1024
MAX_PATCHES = 2
MAX_SOURCE_FILES = 64
PATCH_TIMEOUT_SECONDS = 30.0
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class GateError(ValueError):
    """A fail-closed input, provenance, or patch applicability error."""


@dataclass(frozen=True)
class SourceSpec:
    path: str
    sha256: str


@dataclass(frozen=True)
class PatchSpec:
    path: str
    sha256: str


@dataclass(frozen=True)
class Manifest:
    package: str
    version: str
    sources: tuple[SourceSpec, ...]
    patches: tuple[PatchSpec, ...]
    strip: int
    fuzz: int


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str
    patch_count: int
    source_count: int


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GateError(f"{label} must be an object")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise GateError(f"{label} must be a non-empty string")
    return value


def _safe_relative(value: object, label: str) -> str:
    path = _string(value, label)
    parsed = PurePosixPath(path)
    if (
        parsed.is_absolute()
        or not parsed.parts
        or any(part in ("", ".", "..") for part in parsed.parts)
    ):
        raise GateError(f"{label} must be a safe relative path")
    if "\\" in path:
        raise GateError(f"{label} must use POSIX separators")
    return "/".join(parsed.parts)


def _sha256(value: object, label: str) -> str:
    digest = _string(value, label)
    if SHA256_RE.fullmatch(digest) is None:
        raise GateError(f"{label} must be a lowercase SHA-256 digest")
    return digest


def load_manifest(path: Path) -> Manifest:
    """Load and strictly validate the source provenance manifest."""

    try:
        size = path.stat().st_size
    except OSError as error:
        raise GateError(f"cannot read manifest: {error}") from error
    if size > MAX_MANIFEST_BYTES:
        raise GateError("manifest exceeds its byte bound")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GateError(f"invalid manifest: {error}") from error
    root = _object(document, "manifest")
    if set(root) != {"schema_version", "source", "patches", "patch_options"}:
        raise GateError("manifest has unexpected or missing fields")
    if root["schema_version"] != 1 or isinstance(root["schema_version"], bool):
        raise GateError("manifest schema_version must be 1")

    source = _object(root["source"], "source")
    if set(source) != {"package", "version", "files"}:
        raise GateError("source has unexpected or missing fields")
    package = _string(source["package"], "source.package")
    version = _string(source["version"], "source.version")
    source_rows = source["files"]
    if not isinstance(source_rows, list) or not source_rows:
        raise GateError("source.files must be a non-empty list")
    if len(source_rows) > MAX_SOURCE_FILES:
        raise GateError("source.files exceeds its count bound")
    sources: list[SourceSpec] = []
    source_names: set[str] = set()
    for index, row in enumerate(source_rows):
        item = _object(row, f"source.files[{index}]")
        if set(item) != {"path", "sha256"}:
            raise GateError(f"source.files[{index}] has unexpected fields")
        relative = _safe_relative(item["path"], f"source.files[{index}].path")
        if relative in source_names:
            raise GateError(f"duplicate source path: {relative}")
        source_names.add(relative)
        sources.append(
            SourceSpec(
                relative, _sha256(item["sha256"], f"source.files[{index}].sha256")
            )
        )

    patch_rows = root["patches"]
    if not isinstance(patch_rows, list) or len(patch_rows) != MAX_PATCHES:
        raise GateError(f"patches must contain exactly {MAX_PATCHES} entries")
    patches: list[PatchSpec] = []
    patch_names: set[str] = set()
    for index, row in enumerate(patch_rows):
        item = _object(row, f"patches[{index}]")
        if set(item) != {"path", "sha256"}:
            raise GateError(f"patches[{index}] has unexpected fields")
        relative = _safe_relative(item["path"], f"patches[{index}].path")
        if relative in patch_names:
            raise GateError(f"duplicate patch path: {relative}")
        patch_names.add(relative)
        patches.append(
            PatchSpec(relative, _sha256(item["sha256"], f"patches[{index}].sha256"))
        )

    options = _object(root["patch_options"], "patch_options")
    if (
        set(options) != {"strip", "fuzz"}
        or options["strip"] != 1
        or options["fuzz"] != 0
    ):
        raise GateError("patch_options must require strip=1 and fuzz=0")
    return Manifest(package, version, tuple(sources), tuple(patches), 1, 0)


def _under(root: Path, candidate: Path, label: str) -> Path:
    root_resolved = root.resolve()
    candidate_resolved = candidate.resolve()
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as error:
        raise GateError(f"{label} escapes its root") from error
    return candidate_resolved


def _regular_file(path: Path, label: str, maximum: int) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise GateError(f"{label} is not a regular file")
        if path.stat().st_size > maximum:
            raise GateError(f"{label} exceeds its byte bound")
        return path.read_bytes()
    except OSError as error:
        raise GateError(f"cannot read {label}: {error}") from error


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _patch_targets(payload: bytes, label: str) -> set[str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GateError(f"{label} is not UTF-8 patch text") from error
    targets: set[str] = set()
    for line in text.splitlines():
        if not (line.startswith("--- ") or line.startswith("+++ ")):
            continue
        value = line[4:].split("\t", 1)[0].split(" ", 1)[0]
        if value == "/dev/null":
            continue
        prefix = (
            "a/" if value.startswith("a/") else "b/" if value.startswith("b/") else ""
        )
        if not prefix:
            raise GateError(f"{label} has an unsafe file header")
        targets.add(_safe_relative(value[len(prefix) :], f"{label} target"))
    if not targets:
        raise GateError(f"{label} has no file headers")
    return targets


def _run_patch_dry_run(
    staged: Path, patch: bytes, patch_name: str, strip: int, fuzz: int
) -> None:
    command = [
        "patch",
        "--dry-run",
        f"--fuzz={fuzz}",
        f"-p{strip}",
        "--batch",
        "--forward",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=staged,
            input=patch,
            capture_output=True,
            check=False,
            timeout=PATCH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GateError(f"patch dry-run failed for {patch_name}: {error}") from error
    if completed.returncode != 0:
        detail = (
            (completed.stderr or completed.stdout).decode("utf-8", "replace").strip()
        )
        detail = " ".join(detail.split())[:240]
        raise GateError(
            f"patch dry-run failed for {patch_name}: {detail or 'exit status'}"
        )


def run_gate(source_root: Path, manifest_path: Path = DEFAULT_MANIFEST) -> GateResult:
    """Run the source-only gate against a caller-provided Firefox tree."""

    source_root = source_root.resolve()
    if not source_root.is_dir() or source_root.is_symlink():
        raise GateError("source root is not a directory")
    manifest_path = manifest_path.resolve()
    manifest = load_manifest(manifest_path)
    manifest_root = manifest_path.parent
    patch_payloads: list[tuple[PatchSpec, bytes]] = []
    total_patch_bytes = 0
    for spec in manifest.patches:
        path = _under(manifest_root, manifest_root / spec.path, f"patch {spec.path}")
        payload = _regular_file(path, f"patch {spec.path}", MAX_PATCH_BYTES)
        total_patch_bytes += len(payload)
        if total_patch_bytes > MAX_PATCH_BYTES * MAX_PATCHES:
            raise GateError("patches exceed their aggregate byte bound")
        if _hash(payload) != spec.sha256:
            raise GateError(f"patch hash mismatch: {spec.path}")
        targets = _patch_targets(payload, f"patch {spec.path}")
        source_names = {entry.path for entry in manifest.sources}
        if not targets <= source_names:
            missing = ", ".join(sorted(targets - source_names))
            raise GateError(f"patch {spec.path} targets unmanifested source: {missing}")
        patch_payloads.append((spec, payload))

    source_payloads: list[tuple[SourceSpec, bytes]] = []
    total_source_bytes = 0
    for spec in manifest.sources:
        path = _under(source_root, source_root / spec.path, f"source {spec.path}")
        payload = _regular_file(path, f"source {spec.path}", MAX_SOURCE_FILE_BYTES)
        total_source_bytes += len(payload)
        if total_source_bytes > MAX_TOTAL_SOURCE_BYTES:
            raise GateError("source files exceed their aggregate byte bound")
        if _hash(payload) != spec.sha256:
            raise GateError(f"source hash mismatch: {spec.path}")
        source_payloads.append((spec, payload))

    with tempfile.TemporaryDirectory(prefix="firefox-source-patch-gate-") as directory:
        staged = Path(directory)
        for spec, payload in source_payloads:
            destination = staged / spec.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        for spec, payload in patch_payloads:
            _run_patch_dry_run(
                staged, payload, spec.path, manifest.strip, manifest.fuzz
            )

    return GateResult(
        True,
        f"ESR {manifest.version} source and two patches verified",
        len(patch_payloads),
        len(source_payloads),
    )


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="firefox-source-patch-gate")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    values = parser.parse_args(arguments)
    try:
        result = run_gate(values.source_root, values.manifest)
    except (GateError, OSError, ValueError) as error:
        print(f"FIREFOX_SOURCE_PATCH_GATE FAIL reason={' '.join(str(error).split())}")
        return 1
    print(
        "FIREFOX_SOURCE_PATCH_GATE PASS "
        f"patches={result.patch_count} source_files={result.source_count} fuzz=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
