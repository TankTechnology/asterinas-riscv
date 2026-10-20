#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Admit a Firefox runtime package identity for the audited source patches.

This small gate compares the Firefox row in a rootfs ``packages.lock`` with
the package and version named by ``source_patch_manifest.json``.  It does not
claim that the runtime bytes contain the patches; that claim still requires a
real Debian package and :mod:`package_provenance_gate`.  Its purpose is to
fail closed before a QEMU WebRender run accidentally uses an unrelated ESR
binary.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sys

try:
    from tools.riscv.debian.rootfs.firefox_compat import source_patch_gate
except ModuleNotFoundError:  # Direct invocation from the rootfs builder.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import source_patch_gate  # type: ignore[no-redef]


MAX_LOCK_BYTES = 4 * 1024 * 1024
RUNTIME_ARCHITECTURE = "riscv64"


class GateError(ValueError):
    """A runtime package identity fails the admission contract."""


@dataclass(frozen=True)
class RuntimeResult:
    passed: bool
    reason: str
    package: str
    architecture: str
    version: str
    packages_lock_sha256: str


def _read_lock(path: Path) -> tuple[dict[str, tuple[str, str]], str]:
    try:
        if path.is_symlink() or not path.is_file():
            raise GateError("packages.lock is not a regular file")
        if path.stat().st_size > MAX_LOCK_BYTES:
            raise GateError("packages.lock exceeds its byte bound")
        payload_bytes = path.read_bytes()
        payload = payload_bytes.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise GateError(f"cannot read packages.lock: {error}") from error

    rows: dict[str, tuple[str, str]] = {}
    for line_number, line in enumerate(payload.splitlines(), 1):
        fields = line.split("\t")
        if len(fields) != 3 or any(not field for field in fields):
            raise GateError(f"packages.lock line {line_number} is not name<TAB>arch<TAB>version")
        name, architecture, version = fields
        if name in rows:
            raise GateError(f"duplicate package row: {name}")
        rows[name] = (architecture, version)
    if not rows:
        raise GateError("packages.lock is empty")
    return rows, hashlib.sha256(payload_bytes).hexdigest()


def run_gate(packages_lock: Path, source_manifest: Path) -> RuntimeResult:
    """Require the runtime Firefox identity to match the audited source."""

    try:
        manifest = source_patch_gate.load_manifest(source_manifest.resolve())
    except (OSError, source_patch_gate.GateError) as error:
        raise GateError(f"source manifest rejected: {error}") from error
    rows, lock_sha256 = _read_lock(packages_lock.resolve())
    expected_name = manifest.package
    if expected_name != "firefox-esr":
        raise GateError(f"unexpected source package: {expected_name}")
    try:
        architecture, version = rows[expected_name]
    except KeyError as error:
        raise GateError(f"packages.lock is missing {expected_name}") from error
    if architecture != RUNTIME_ARCHITECTURE:
        raise GateError(
            f"architecture mismatch: {architecture}; expected {RUNTIME_ARCHITECTURE}"
        )
    if version != manifest.version:
        raise GateError(f"version mismatch: {version}; expected {manifest.version}")
    return RuntimeResult(
        True,
        "runtime package identity matches source patch manifest",
        expected_name,
        architecture,
        version,
        lock_sha256,
    )


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="firefox-runtime-provenance-gate")
    parser.add_argument("--packages-lock", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    values = parser.parse_args(arguments)
    try:
        result = run_gate(values.packages_lock, values.source_manifest)
    except (GateError, OSError, ValueError) as error:
        print(f"FIREFOX_RUNTIME_PROVENANCE FAIL reason={' '.join(str(error).split())}")
        return 1
    print(
        "FIREFOX_RUNTIME_PROVENANCE PASS "
        f"package={result.package} architecture={result.architecture} "
        f"version={result.version} packages_lock_sha256={result.packages_lock_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
