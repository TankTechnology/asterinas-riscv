#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Fail-closed static audit of a staged Firefox ESR trust chain."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


class CheckFailure(RuntimeError):
    """A staged root cannot provide one statically usable Firefox trust chain."""


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
    firefox = regular_file(root, "usr/lib/firefox-esr/libxul.so")
    nss = regular_file(root, "usr/lib/firefox-esr/libnss3.so")
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
        root / "usr/lib/firefox-esr/libnssckbi.so",
        root / "usr/lib/riscv64-linux-gnu/libnssckbi.so",
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
        trust_mode = f"external:{module.relative_to(root)}"
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
