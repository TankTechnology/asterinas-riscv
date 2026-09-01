#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Install the frozen RISC-V JIT Firefox overlay into a staged root."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


MARKER = Path("usr/share/asterinas/firefox-riscv-jit-overlay.json")
JIT_DEFAULT_COMMIT = "e305b081ec59a4711183b7386a310b313893f881"


class OverlayError(RuntimeError):
    """The overlay input or staged root violates the frozen contract."""


@dataclass(frozen=True)
class Package:
    role: str
    filename: str
    package: str
    version: str
    sha1: str
    sha256: str


PACKAGES = (
    Package(
        "browser",
        "firefox_143.0.3-1_riscv64.deb",
        "firefox",
        "143.0.3-1",
        "584d283a9aadc96495aacc692b5afe16da7a6222",
        "a04355d86def0376134ba5385f56d0dfba813e3349ddbcce09f6b6f28c60a4de",
    ),
    Package(
        "nss",
        "libnss3_3.116-1_riscv64.deb",
        "libnss3",
        "2:3.116-1",
        "84e9a7d8ab4c214a90200ff50ce61bdc703e5029",
        "2e4faf833987767740d75e73ac0c16602c560cb6de83bc17319764fe332d9eeb",
    ),
    Package(
        "vpx",
        "libvpx11_1.15.2-1_riscv64.deb",
        "libvpx11",
        "1.15.2-1",
        "bae8c96685369608ec410d97186d90daf5b1265e",
        "bc56d2762130d32f260347cf43ba482827ef85e0d9d84307a3460115f4c01f57",
    ),
)

RUNTIME_FILES = {
    "firefox": "usr/lib/firefox/firefox",
    "libxul": "usr/lib/firefox/libxul.so",
    "nss": "usr/lib/riscv64-linux-gnu/libnss3.so",
    "nssckbi": "usr/lib/riscv64-linux-gnu/libnssckbi.so",
    "vpx": "usr/lib/riscv64-linux-gnu/libvpx.so.11.0.1",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _run(*argv: str, cwd: Path | None = None) -> str:
    result = subprocess.run(argv, cwd=cwd, check=False, capture_output=True, text=True)
    if result.returncode:
        raise OverlayError(
            f"command failed ({result.returncode}): {' '.join(argv)}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def _regular(path: Path, *, executable: bool = False) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or (executable and not metadata.st_mode & 0o111):
        raise OverlayError(f"unsafe or unusable overlay runtime file: {path}")


def _extract_data(deb: Path, root: Path) -> None:
    members = _run("ar", "t", os.fspath(deb)).splitlines()
    data_members = [name for name in members if name.startswith("data.tar.")]
    if len(data_members) != 1:
        raise OverlayError(f"Debian package has no unique data archive: {deb}")
    with tempfile.TemporaryDirectory(prefix="asterinas-firefox-overlay-") as directory:
        archive = Path(directory) / data_members[0]
        with archive.open("wb") as output:
            result = subprocess.run(
                ["ar", "p", os.fspath(deb), data_members[0]],
                check=False,
                stdout=output,
                stderr=subprocess.PIPE,
            )
        if result.returncode:
            raise OverlayError(f"failed to extract Debian data archive: {deb}")
        _run(
            "tar",
            "--extract",
            "--file",
            os.fspath(archive),
            "--directory",
            os.fspath(root),
            "--numeric-owner",
        )


def install(root: Path, package_paths: Sequence[Path]) -> dict[str, object]:
    root = root.resolve()
    if not root.is_dir() or root == Path("/"):
        raise OverlayError("overlay root must be an existing non-host directory")
    if len(package_paths) != len(PACKAGES):
        raise OverlayError("overlay requires exactly the frozen browser, NSS, and VPX packages")

    by_name = {path.name: path.resolve() for path in package_paths}
    if set(by_name) != {package.filename for package in PACKAGES}:
        raise OverlayError("overlay package filenames do not match the frozen set")
    for package in PACKAGES:
        path = by_name[package.filename]
        _regular(path)
        if sha256(path) != package.sha256:
            raise OverlayError(f"overlay package hash mismatch: {package.filename}")

    marker = root / MARKER
    if marker.exists() or marker.is_symlink():
        raise OverlayError("Firefox JIT overlay marker already exists")
    for package in PACKAGES:
        _extract_data(by_name[package.filename], root)

    launcher = root / "usr/bin/firefox"
    if not launcher.is_symlink() or os.readlink(launcher) != "../lib/firefox/firefox":
        raise OverlayError("Firefox overlay launcher is not the packaged symlink")
    for name, relative in RUNTIME_FILES.items():
        _regular(root / relative, executable=name == "firefox")

    # Preserve the same enterprise security policy as the frozen ESR image.
    source_policy = root / "usr/lib/firefox-esr/distribution/policies.json"
    target_policy = root / "usr/share/firefox/distribution/policies.json"
    _regular(source_policy)
    target_policy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_policy, target_policy)

    manifest: dict[str, object] = {
        "architecture": "riscv64",
        "jit_default_commit": JIT_DEFAULT_COMMIT,
        "packages": [
            {
                "filename": package.filename,
                "package": package.package,
                "role": package.role,
                "sha1": package.sha1,
                "sha256": package.sha256,
                "version": package.version,
            }
            for package in PACKAGES
        ],
        "runtime_files": {
            name: {"path": relative, "sha256": sha256(root / relative)}
            for name, relative in sorted(RUNTIME_FILES.items())
        },
        "schema_version": 1,
        "trust_mode": "system-nss-jit-overlay",
    }
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    marker.chmod(0o644)
    return manifest


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("packages", nargs=3, type=Path)
    values = parser.parse_args(arguments)
    try:
        manifest = install(values.root, values.packages)
    except (OSError, OverlayError) as error:
        parser.error(str(error))
    print(
        "FIREFOX_JIT_OVERLAY_PASS "
        f"version={PACKAGES[0].version} files={len(manifest['runtime_files'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
