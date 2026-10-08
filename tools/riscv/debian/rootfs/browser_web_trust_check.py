#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Fail-closed static audit of a staged Firefox ESR trust chain."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


class CheckFailure(RuntimeError):
    """A staged root cannot provide one statically usable Firefox trust chain."""


OVERLAY_MARKER = "usr/share/asterinas/firefox-riscv-jit-overlay.json"
OVERLAY_COMMIT = "e305b081ec59a4711183b7386a310b313893f881"
OVERLAY_RUNTIME_PATHS = {
    "firefox": "usr/lib/firefox/firefox",
    "libxul": "usr/lib/firefox/libxul.so",
    "nss": "usr/lib/riscv64-linux-gnu/libnss3.so",
    "nssckbi": "usr/lib/riscv64-linux-gnu/libnssckbi.so",
    "vpx": "usr/lib/riscv64-linux-gnu/libvpx.so.11.0.1",
}
OVERLAY_PACKAGES = (
    {
        "filename": "firefox_143.0.3-1_riscv64.deb",
        "package": "firefox",
        "role": "browser",
        "sha1": "584d283a9aadc96495aacc692b5afe16da7a6222",
        "sha256": "a04355d86def0376134ba5385f56d0dfba813e3349ddbcce09f6b6f28c60a4de",
        "version": "143.0.3-1",
    },
    {
        "filename": "libnss3_3.116-1_riscv64.deb",
        "package": "libnss3",
        "role": "nss",
        "sha1": "84e9a7d8ab4c214a90200ff50ce61bdc703e5029",
        "sha256": "2e4faf833987767740d75e73ac0c16602c560cb6de83bc17319764fe332d9eeb",
        "version": "2:3.116-1",
    },
    {
        "filename": "libvpx11_1.15.2-1_riscv64.deb",
        "package": "libvpx11",
        "role": "vpx",
        "sha1": "bae8c96685369608ec410d97186d90daf5b1265e",
        "sha256": "bc56d2762130d32f260347cf43ba482827ef85e0d9d84307a3460115f4c01f57",
        "version": "1.15.2-1",
    },
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def regular_file(root: Path, relative: str) -> Path:
    path = root / relative
    require(path.is_file() and not path.is_symlink(), f"missing/non-regular {relative}")
    require(path.resolve().is_relative_to(root.resolve()), f"path escapes root: {relative}")
    return path


def output(*argv: str) -> str:
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    require(
        result.returncode == 0,
        f"command failed: {' '.join(argv)}: {result.stderr.strip()}",
    )
    return result.stdout


def require_riscv_elf(path: Path) -> None:
    description = output("file", "-b", str(path))
    require(
        "ELF 64-bit" in description and "RISC-V" in description,
        f"not RISC-V ELF: {path}",
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def overlay_runtime(root: Path) -> dict[str, Path] | None:
    marker = root / OVERLAY_MARKER
    if not marker.exists():
        return None
    marker = regular_file(root, OVERLAY_MARKER)
    try:
        manifest = json.loads(marker.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CheckFailure("Firefox JIT overlay marker is malformed") from error
    require(
        isinstance(manifest, dict)
        and set(manifest) == {
            "architecture",
            "jit_default_commit",
            "packages",
            "runtime_files",
            "schema_version",
            "trust_mode",
        }
        and manifest["architecture"] == "riscv64"
        and manifest["jit_default_commit"] == OVERLAY_COMMIT
        and manifest["schema_version"] == 1
        and manifest["trust_mode"] == "system-nss-jit-overlay",
        "Firefox JIT overlay identity is invalid",
    )
    require(
        manifest["packages"] == list(OVERLAY_PACKAGES),
        "Firefox JIT overlay package identities are invalid",
    )
    runtime_files = manifest["runtime_files"]
    require(
        isinstance(runtime_files, dict)
        and set(runtime_files) == set(OVERLAY_RUNTIME_PATHS),
        "Firefox JIT overlay runtime file set is invalid",
    )
    resolved: dict[str, Path] = {}
    for name, relative in OVERLAY_RUNTIME_PATHS.items():
        identity = runtime_files[name]
        require(
            isinstance(identity, dict)
            and set(identity) == {"path", "sha256"}
            and identity["path"] == relative
            and isinstance(identity["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", identity["sha256"]) is not None,
            f"Firefox JIT overlay identity is malformed: {name}",
        )
        path = regular_file(root, relative)
        require(
            sha256(path) == identity["sha256"],
            f"Firefox JIT overlay runtime hash mismatch: {name}",
        )
        resolved[name] = path
    return resolved


def package_installed(status: str, name: str) -> bool:
    return bool(
        re.search(
            rf"(?ms)^Package: {re.escape(name)}\n(?:(?!\n\n).)*^Status: install ok installed$",
            status,
        )
    )


def check_root(root: Path) -> str:
    root = root.resolve()
    require(root.is_dir(), "root must be an existing directory")

    status_path = regular_file(root, "var/lib/dpkg/status")
    overlay = overlay_runtime(root)
    firefox = (
        overlay["libxul"]
        if overlay is not None
        else regular_file(root, "usr/lib/firefox-esr/libxul.so")
    )
    nss = (
        overlay["nss"]
        if overlay is not None
        else regular_file(root, "usr/lib/firefox-esr/libnss3.so")
    )
    ca_bundle = regular_file(root, "etc/ssl/certs/ca-certificates.crt")

    status = status_path.read_text(encoding="utf-8")
    require(package_installed(status, "firefox-esr"), "firefox-esr not installed")
    require(
        package_installed(status, "ca-certificates"),
        "ca-certificates not installed",
    )

    require_riscv_elf(firefox)
    require_riscv_elf(nss)
    dynamic = output("readelf", "-d", str(firefox))
    symbols = output("readelf", "-Ws", str(firefox))
    nss_symbols = output("readelf", "-Ws", str(nss))
    require("Shared library: [libnss3.so]" in dynamic, "libxul does not load libnss3")
    require("NSS_Initialize@" in symbols, "libxul does not import NSS_Initialize")
    require(
        re.search(r"\bNSS_Initialize@@?NSS_3\.2\b", nss_symbols) is not None,
        "private libnss3 does not export NSS_Initialize",
    )

    pem = ca_bundle.read_text(encoding="ascii")
    cert_count = pem.count("-----BEGIN CERTIFICATE-----")
    require(cert_count >= 100, f"system CA bundle too small: {cert_count} certificates")

    external_candidates = (
        (overlay["nssckbi"],)
        if overlay is not None
        else (
            root / "usr/lib/firefox-esr/libnssckbi.so",
            root / "usr/lib/riscv64-linux-gnu/libnssckbi.so",
        )
    )
    external = [path for path in external_candidates if path.exists()]
    if external:
        require(len(external) == 1, "ambiguous external CKBI modules")
        module = external[0]
        require(
            module.is_file() and not module.is_symlink(),
            "external CKBI is not a regular file",
        )
        require_riscv_elf(module)
        require(
            "C_GetFunctionList" in output("readelf", "-Ws", str(module)),
            "external CKBI does not export C_GetFunctionList",
        )
        trust_mode = (
            "system-nss-jit-overlay"
            if overlay is not None
            else f"external:{module.relative_to(root)}"
        )
    else:
        strings = output("strings", "-a", str(firefox)).splitlines()
        required_xul_evidence = {
            "LoadLoadableRoots failed",
            "Root Certs",
            "nssckbi",
            "ISRG Root X1",
            "DigiCert Global Root G2",
            "GlobalSign Root R46",
        }
        missing = sorted(required_xul_evidence.difference(strings))
        require(
            not missing,
            f"no external CKBI and incomplete XUL trust anchors: {missing}",
        )
        trust_mode = "embedded-xul"

    return (
        "FIREFOX_TRUST_PASS "
        f"mode={trust_mode} ca_certificates={cert_count} "
        "firefox=installed ca_package=installed riscv_elf=1 nss_loader=1"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    values = parser.parse_args()
    try:
        print(check_root(values.root))
    except CheckFailure as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
