#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Strict multi-source provenance primitives for Debian browser M5."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import subprocess


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class SignedSource:
    role: str
    mirror_url: str
    suite: str
    codename: str
    release_suite: str
    version_pattern: str
    components: tuple[str, ...]

    @property
    def inrelease_url(self) -> str:
        return f"{self.mirror_url}/dists/{self.suite}/InRelease"


BASE_SOURCE = SignedSource(
    role="base",
    mirror_url="https://mirrors.tuna.tsinghua.edu.cn/debian",
    suite="trixie",
    codename="trixie",
    release_suite="stable",
    version_pattern=r"13\.(?:0|[1-9][0-9]*)",
    components=("main", "contrib", "non-free-firmware", "non-free"),
)
SECURITY_SOURCE = SignedSource(
    role="security",
    mirror_url="https://security.debian.org/debian-security",
    suite="trixie-security",
    codename="trixie-security",
    release_suite="stable-security",
    version_pattern=r"13",
    components=(
        "updates/main",
        "updates/contrib",
        "updates/non-free-firmware",
        "updates/non-free",
    ),
)
M5_SOURCES = (BASE_SOURCE, SECURITY_SOURCE)


def verify_inrelease(source: SignedSource, inrelease: Path, keyring: Path) -> str:
    """Verify one signature and its exact release identity."""

    subprocess.run(
        ["gpgv", "--keyring", str(keyring), str(inrelease)],
        check=True,
        capture_output=True,
    )
    fields = _single_fields(
        inrelease.read_text(encoding="utf-8"),
        ("Suite", "Version", "Codename", "Architectures", "Components"),
    )
    if fields.get("Suite") != source.release_suite:
        raise ValueError(f"unexpected {source.role} Suite")
    if fields.get("Codename") != source.codename:
        raise ValueError(f"unexpected {source.role} Codename")
    version = fields.get("Version")
    if version is None or re.fullmatch(source.version_pattern, version) is None:
        raise ValueError(f"unexpected {source.role} Version")
    architectures = fields.get("Architectures", "").split()
    if "riscv64" not in architectures or len(architectures) != len(set(architectures)):
        raise ValueError(f"unexpected {source.role} Architectures")
    if tuple(fields.get("Components", "").split()) != source.components:
        raise ValueError(f"unexpected {source.role} Components")
    return version


def require_unchanged(retained: Path, current: Path, role: str) -> None:
    """Reject signed metadata drift even when both documents have valid signatures."""

    if _sha256(retained.read_bytes()) != _sha256(current.read_bytes()):
        raise ValueError(f"{role} InRelease changed during build")


def source_for_apt_list(filename: str, sources: tuple[SignedSource, ...] = M5_SOURCES) -> SignedSource:
    """Map an apt Packages filename to exactly one configured signed source."""

    matches = [
        source
        for source in sources
        if f"_dists_{source.suite}_main_binary-riscv64_Packages" in filename
        or f"_dists_{source.suite}_main_binary-all_Packages" in filename
    ]
    if len(matches) != 1:
        raise ValueError(f"apt Packages list has {len(matches)} source owners: {filename}")
    return matches[0]


def authenticate_packages(index: bytes, release_path: str, inrelease: bytes) -> None:
    """Bind decompressed Packages bytes to exactly one InRelease SHA256 row."""

    expected_hash = _sha256(index)
    expected_size = str(len(index))
    matches = 0
    in_sha256 = False
    for line in inrelease.decode("utf-8").splitlines():
        if line == "SHA256:":
            in_sha256 = True
            continue
        if in_sha256 and line and not line[0].isspace():
            in_sha256 = False
        fields = line.split()
        if in_sha256 and fields == [expected_hash, expected_size, release_path]:
            matches += 1
    if matches != 1:
        raise ValueError("Packages index is not uniquely authenticated by its InRelease")


def signed_sources_manifest(sources: tuple[SignedSource, ...], files: dict[str, Path]) -> list[dict[str, str]]:
    """Return a deterministic schema-5 signed_sources fragment."""

    if set(files) != {source.role for source in sources}:
        raise ValueError("signed source files do not match configured roles")
    rows = [
        {
            "role": source.role,
            "mirror_url": source.mirror_url,
            "suite": source.suite,
            "inrelease_url": source.inrelease_url,
            "inrelease_sha256": _sha256(files[source.role].read_bytes()),
        }
        for source in sources
    ]
    rows.sort(key=lambda row: row["role"])
    if len({row["role"] for row in rows}) != len(rows):
        raise ValueError("duplicate signed source role")
    return rows


def _single_fields(document: str, names: tuple[str, ...]) -> dict[str, str]:
    values: dict[str, list[str]] = {name: [] for name in names}
    for line in document.splitlines():
        for name in names:
            prefix = f"{name}: "
            if line.startswith(prefix):
                values[name].append(line[len(prefix) :])
    duplicates = [name for name, found in values.items() if len(found) > 1]
    if duplicates:
        raise ValueError(f"duplicate InRelease fields: {duplicates}")
    return {name: found[0] for name, found in values.items() if found}


def _sha256(data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    assert _SHA256_RE.fullmatch(digest)
    return digest
