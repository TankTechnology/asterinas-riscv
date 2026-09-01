#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Fail-closed preflight for an extracted Firefox online root filesystem."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


class CheckFailure(RuntimeError):
    """The online root cannot satisfy the frozen network/TLS contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def regular(root: Path, relative: str, *, executable: bool = False) -> Path:
    path = root / relative
    require(path.is_file() and not path.is_symlink(), f"missing/non-regular {relative}")
    require(path.resolve().is_relative_to(root.resolve()), f"path escapes root: {relative}")
    if executable:
        require(os.access(path, os.X_OK), f"not executable: {relative}")
    return path


def riscv_elf(path: Path) -> None:
    result = subprocess.run(
        ["file", "-b", str(path)], check=False, capture_output=True, text=True
    )
    require(result.returncode == 0, f"file failed: {path}")
    require(
        "ELF 64-bit" in result.stdout and "RISC-V" in result.stdout,
        f"not RISC-V ELF: {path}",
    )


def package_installed(status: str, name: str) -> bool:
    return bool(
        re.search(
            rf"(?ms)^Package: {re.escape(name)}\n(?:(?!\n\n).)*^Status: install ok installed$",
            status,
        )
    )


def check_root(root: Path, trust_checker: Path) -> str:
    root = root.resolve()
    require(root.is_dir(), "root must be an existing directory")
    status = regular(root, "var/lib/dpkg/status").read_text(encoding="utf-8")
    for package in ("firefox-esr", "ca-certificates", "curl"):
        require(package_installed(status, package), f"{package} not installed")
    overlay = root / "usr/share/asterinas/firefox-riscv-jit-overlay.json"
    firefox_relative = (
        "usr/lib/firefox/firefox"
        if overlay.is_file() and not overlay.is_symlink()
        else "usr/lib/firefox-esr/firefox-esr"
    )
    for relative in ("usr/bin/getent", "usr/bin/curl", firefox_relative):
        riscv_elf(regular(root, relative, executable=True))
    launcher_relative = (
        "usr/bin/firefox"
        if firefox_relative.startswith("usr/lib/firefox/")
        else "usr/bin/firefox-esr"
    )
    launcher = root / launcher_relative
    expected_launcher = (
        "../lib/firefox/firefox"
        if launcher_relative == "usr/bin/firefox"
        else "../lib/firefox-esr/firefox-esr"
    )
    require(
        launcher.is_symlink(),
        f"{launcher_relative} must be the packaged symlink",
    )
    require(
        os.readlink(launcher) == expected_launcher,
        "unexpected Firefox symlink target",
    )
    nsswitch = regular(root, "etc/nsswitch.conf").read_text(encoding="utf-8")
    hosts = re.search(r"(?m)^hosts:\s+(.+)$", nsswitch)
    require(hosts is not None, "nsswitch hosts database missing")
    methods = hosts.group(1).split()
    require("files" in methods and "dns" in methods, "nsswitch hosts must contain files and dns")
    resolv = regular(root, "etc/resolv.conf").read_text(encoding="ascii")
    resolver_lines = [
        line.strip()
        for line in resolv.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    require(
        resolver_lines == ["nameserver 10.0.2.3"],
        f"resolver is not the frozen slirp DNS contract: {resolver_lines}",
    )
    checker = trust_checker.resolve()
    require(checker.is_file(), "trust checker unavailable")
    trust = subprocess.run(
        [sys.executable, str(checker), str(root)],
        check=False,
        capture_output=True,
        text=True,
    )
    require(trust.returncode == 0, f"Firefox trust preflight failed: {trust.stderr.strip()}")
    expected_trust = (
        "mode=system-nss-jit-overlay"
        if launcher_relative == "usr/bin/firefox"
        else "mode=embedded-xul"
    )
    require(
        trust.stdout.count("FIREFOX_TRUST_PASS ") == 1
        and expected_trust in trust.stdout,
        "trust checker did not emit exactly one expected PASS",
    )
    return (
        "FIREFOX_ONLINE_ROOTFS_PASS resolver=10.0.2.3 nsswitch=files,dns "
        "curl=riscv64 getent=riscv64 firefox=riscv64 trust_static=pass "
        "runtime_proven=0"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--trust-checker", type=Path, required=True)
    args = parser.parse_args()
    print(check_root(args.root, args.trust_checker))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CheckFailure as error:
        print(f"FIREFOX_ONLINE_ROOTFS_FAIL reason={error}", file=sys.stderr)
        raise SystemExit(1)
