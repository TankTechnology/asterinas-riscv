#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Publish and validate identities for packaged RISC-V LTP initramfs files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from ltp_suite import suite_names


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _document(
    *,
    suite: str,
    initramfs: Path,
    manifest: Path,
    unavailable: Path,
) -> dict[str, object]:
    if suite not in suite_names():
        raise ValueError(f"unknown LTP suite: {suite}")
    return {
        "schema_version": 1,
        "suite": suite,
        "initramfs_sha256": _sha256(initramfs),
        "manifest_sha256": _sha256(manifest),
        "unavailable_sha256": _sha256(unavailable),
    }


def publish_package_identity(
    *,
    suite: str,
    initramfs: Path,
    manifest: Path,
    unavailable: Path,
    output: Path,
) -> None:
    """Publishes an atomic identity for one complete LTP package."""

    document = _document(
        suite=suite,
        initramfs=initramfs,
        manifest=manifest,
        unavailable=unavailable,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as identity_file:
            json.dump(document, identity_file, indent=2, sort_keys=True)
            identity_file.write("\n")
            os.fchmod(identity_file.fileno(), 0o644)
            identity_file.flush()
            os.fsync(identity_file.fileno())
        os.replace(temporary, output)
        directory_descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def validate_package_identity(
    *,
    suite: str,
    initramfs: Path,
    manifest: Path,
    unavailable: Path,
    identity: Path,
) -> None:
    """Validates that an identity describes the supplied package files."""

    loaded = json.loads(identity.read_text())
    if not isinstance(loaded, Mapping):
        raise ValueError("package identity must be an object")
    expected = _document(
        suite=suite,
        initramfs=initramfs,
        manifest=manifest,
        unavailable=unavailable,
    )
    for name, value in expected.items():
        if loaded.get(name) != value:
            raise ValueError(f"package identity has invalid {name}")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    publish = subparsers.add_parser("publish")
    publish.add_argument("--suite", choices=suite_names(), required=True)
    publish.add_argument("--initramfs", type=Path, required=True)
    publish.add_argument("--manifest", type=Path, required=True)
    publish.add_argument("--unavailable", type=Path, required=True)
    publish.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command != "publish":
        raise AssertionError(f"unhandled command: {args.command}")
    publish_package_identity(
        suite=args.suite,
        initramfs=args.initramfs,
        manifest=args.manifest,
        unavailable=args.unavailable,
        output=args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
