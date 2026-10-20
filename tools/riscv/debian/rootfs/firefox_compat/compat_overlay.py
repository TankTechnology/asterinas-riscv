#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Install a provenance-verified Firefox package into a development root.

This is intentionally a separate, explicitly private overlay path.  It does
not alter dpkg's database or claim that the package came from Debian's signed
binary repository.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
from typing import Any

try:
    from tools.riscv.debian.rootfs.firefox_compat import package_provenance_gate
except ModuleNotFoundError:  # Direct invocation from the rootfs builder.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import package_provenance_gate  # type: ignore[no-redef]


MARKER = Path("usr/share/asterinas/firefox-riscv-compat-overlay.json")
REQUIRED_RUNTIME = (
    Path("usr/lib/firefox-esr/firefox-esr"),
    Path("usr/lib/firefox-esr/libxul.so"),
)


class OverlayError(ValueError):
    """An overlay archive or root violates the development contract."""


def _run(argv: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OverlayError(f"command failed: {' '.join(argv)}: {error}") from error


def _data_archive(package: Path, output: Path) -> None:
    listed = _run(["ar", "t", str(package)])
    if listed.returncode != 0:
        raise OverlayError("cannot list Debian package members")
    members = [line for line in listed.stdout.splitlines() if line.startswith("data.tar.")]
    if len(members) != 1:
        raise OverlayError("Debian package must contain one data archive")
    with output.open("wb") as stream:
        extracted = subprocess.run(
            ["ar", "p", str(package), members[0]],
            check=False,
            stdout=stream,
            stderr=subprocess.PIPE,
            text=False,
            timeout=60.0,
        )
    if extracted.returncode != 0:
        raise OverlayError("cannot extract Debian data archive")


def _validate_member_names(archive: Path) -> None:
    listed = _run(["tar", "--list", "--file", str(archive)])
    if listed.returncode != 0:
        raise OverlayError("cannot list Debian data archive")
    for raw in listed.stdout.splitlines():
        name = raw.rstrip("/")
        parsed = PurePosixPath(name)
        if (
            not name
            or parsed.is_absolute()
            or any(part in {"", ".", ".."} for part in parsed.parts)
        ):
            raise OverlayError(f"unsafe data archive member: {name!r}")


def _validate_staging(staging: Path) -> None:
    staging = staging.resolve()
    for path in staging.rglob("*"):
        if path.is_symlink():
            target = (path.parent / os.readlink(path)).resolve()
            try:
                target.relative_to(staging)
            except ValueError as error:
                raise OverlayError(f"symlink escapes staging: {path}") from error
        elif not (path.is_dir() or path.is_file()):
            raise OverlayError(f"unsupported staged file type: {path}")


def _extract(package: Path, staging: Path) -> None:
    archive = staging.parent / (staging.name + ".data")
    try:
        _data_archive(package, archive)
        _validate_member_names(archive)
        extracted = _run(
            [
                "tar",
                "--extract",
                "--no-same-owner",
                "--no-same-permissions",
                "--file",
                str(archive),
                "--directory",
                str(staging),
            ],
            timeout=120.0,
        )
        if extracted.returncode != 0:
            raise OverlayError("cannot extract Debian data archive")
        _validate_staging(staging)
    finally:
        archive.unlink(missing_ok=True)


def _validate_root(root: Path) -> Path:
    root = root.resolve()
    if root == Path("/") or not root.is_dir() or root.is_symlink():
        raise OverlayError("overlay root must be an existing non-host directory")
    return root


def _validate_required_runtime(staging: Path) -> None:
    for relative in REQUIRED_RUNTIME:
        path = staging / relative
        if not path.is_file() or path.is_symlink():
            raise OverlayError(f"runtime file is missing or unsafe: {relative}")
    executable = staging / REQUIRED_RUNTIME[0]
    if not executable.stat().st_mode & stat.S_IXUSR:
        raise OverlayError("Firefox runtime is not executable")


def _validate_destination_parents(root: Path, staging: Path) -> None:
    for source in staging.rglob("*"):
        relative = source.relative_to(staging)
        destination = root / relative
        current = root
        for part in relative.parts[:-1]:
            current /= part
            if current.exists() or current.is_symlink():
                if current.is_symlink() or not current.is_dir():
                    raise OverlayError(f"overlay destination parent is unsafe: {current}")

        if destination.exists() and source.is_dir() and not destination.is_dir():
            raise OverlayError(f"overlay destination type mismatch: {destination}")


def _merge(root: Path, staging: Path) -> None:
    for source in sorted(staging.rglob("*"), key=lambda path: len(path.parts)):
        relative = source.relative_to(staging)
        destination = root / relative
        if source.is_dir() and not source.is_symlink():
            destination.mkdir(exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            if destination.is_dir() and not destination.is_symlink():
                raise OverlayError(f"overlay cannot replace directory: {destination}")
            destination.unlink()
        if source.is_symlink():
            destination.symlink_to(os.readlink(source))
        else:
            shutil.copy2(source, destination)


def _marker(provenance: Path) -> dict[str, Any]:
    document = json.loads(provenance.read_text(encoding="utf-8"))
    return {
        "package": document["package"],
        "patches": document["patches"],
        "patch_count": len(document["patches"]),
        "schema_version": 1,
        "source_manifest": document["source_manifest"],
        "trust_mode": "source-patched-package-overlay",
    }


def install(
    root: Path,
    package: Path,
    provenance: Path,
    compat_root: Path,
) -> package_provenance_gate.PackageResult:
    """Verify and merge one patched package into a disposable root directory."""

    root = _validate_root(root)
    marker = root / MARKER
    if marker.exists() or marker.is_symlink():
        raise OverlayError("Firefox compatibility marker already exists")
    try:
        result = package_provenance_gate.run_gate(package, provenance, compat_root)
    except package_provenance_gate.GateError as error:
        raise OverlayError(f"package provenance rejected: {error}") from error
    with tempfile.TemporaryDirectory(prefix="firefox-compat-overlay-") as directory:
        staging = Path(directory)
        _extract(package.resolve(), staging)
        _validate_required_runtime(staging)
        _validate_destination_parents(root, staging)
        _merge(root, staging)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(_marker(provenance), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    marker.chmod(0o644)
    return result


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="firefox-compat-overlay")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--compat-root", type=Path, required=True)
    values = parser.parse_args(arguments)
    try:
        result = install(values.root, values.package, values.provenance, values.compat_root)
    except (OSError, OverlayError, ValueError) as error:
        print(f"FIREFOX_COMPAT_OVERLAY FAIL reason={' '.join(str(error).split())}")
        return 1
    print(
        "FIREFOX_COMPAT_OVERLAY PASS "
        f"package={result.package} version={result.version} patches={result.patch_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
