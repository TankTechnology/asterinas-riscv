#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Fail-closed static admission checks for the Debian Firefox M5 stage root."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


class CheckFailure(RuntimeError):
    """The staged root cannot satisfy the Firefox M5 contract."""


MAX_PROBE_ASSET_BYTES = 64 * 1024
WEBM_EBML_HEADER = bytes.fromhex("1a45dfa3")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def regular(root: Path, relative: str, *, executable: bool = False) -> Path:
    """Return a non-symlink regular file whose resolved path stays in root."""

    path = root / relative
    root_resolved = root.resolve()
    resolved = path.resolve(strict=False)
    require(resolved.is_relative_to(root_resolved), f"path escapes root: {relative}")
    require(path.is_file() and not path.is_symlink(), f"missing/non-regular {relative}")
    if executable:
        require(os.access(path, os.X_OK), f"not executable: {relative}")
    return path


def package_installed(status: str, name: str) -> bool:
    return bool(
        re.search(
            rf"(?ms)^Package: {re.escape(name)}\n(?:(?!\n\n).)*^Status: install ok installed$",
            status,
        )
    )


def riscv_elf(path: Path) -> None:
    result = subprocess.run(
        ["file", "-b", str(path)], check=False, capture_output=True, text=True
    )
    require(result.returncode == 0, f"file failed: {path}")
    require(
        "ELF 64-bit" in result.stdout and "RISC-V" in result.stdout,
        f"not RISC-V ELF: {path}",
    )


def check_root(root: Path) -> str:
    """Validate the staged Firefox M5 root and return its exact PASS marker."""

    root = root.resolve()
    require(root.is_dir(), "root must be an existing directory")
    status = regular(root, "var/lib/dpkg/status").read_text(encoding="utf-8")
    require(package_installed(status, "firefox-esr"), "firefox-esr is not installed")
    require(not package_installed(status, "netsurf-gtk"), "netsurf-gtk is installed")

    launcher = root / "usr/bin/firefox-esr"
    require(launcher.is_symlink(), "usr/bin/firefox-esr must be a symlink")
    require(
        os.readlink(launcher) == "../lib/firefox-esr/firefox-esr",
        "unexpected firefox-esr symlink target",
    )
    firefox = regular(root, "usr/lib/firefox-esr/firefox-esr", executable=True)
    riscv_elf(firefox)

    html = regular(root, "usr/share/asterinas/browser-m5/index.html")
    video = regular(root, "usr/share/asterinas/browser-m5/browser-m5.webm")
    for asset in (html, video):
        require(
            asset.stat().st_size <= MAX_PROBE_ASSET_BYTES,
            f"browser M5 asset exceeds size limit: {asset.name}",
        )
    html_text = html.read_text(encoding="utf-8").lower()
    require(
        "http://" not in html_text and "https://" not in html_text,
        "browser M5 HTML has external URL",
    )
    require(
        video.read_bytes().startswith(WEBM_EBML_HEADER),
        "browser M5 fixture is not WebM",
    )

    launcher_script = regular(
        root, "usr/lib/asterinas/browser-m5-firefox", executable=True
    )
    launcher_text = launcher_script.read_text(encoding="utf-8")
    require("--marionette" in launcher_text, "Firefox launcher lacks --marionette")
    require(
        "--no-sandbox" not in launcher_text, "Firefox launcher contains --no-sandbox"
    )
    for relative in (
        "usr/lib/asterinas/browser-m5-marionette-gate",
        "usr/lib/asterinas/browser-m5-window-observer",
        "usr/lib/asterinas/browser-m5-network-observer",
        "usr/lib/asterinas/browser-m5-startup-evidence",
    ):
        regular(root, relative, executable=True)

    service = regular(root, "etc/systemd/system/asterinas-browser-m5.service")
    service_text = service.read_text(encoding="utf-8")
    for required in ("User=asterinas", "PrivateNetwork=yes", "NoNewPrivileges=yes"):
        require(required in service_text, f"Firefox service lacks {required}")
    require(
        "--marionette" in launcher_text, "Firefox service launcher lacks Marionette"
    )
    startup_service = regular(
        root, "etc/systemd/system/asterinas-browser-m5-startup.service"
    )
    startup_service_text = startup_service.read_text(encoding="utf-8")
    require(
        "ExecStart=/usr/lib/asterinas/browser-m5-startup-evidence"
        in startup_service_text,
        "Firefox startup service lacks its evidence helper",
    )

    return "FIREFOX_M5_ROOTFS_PASS firefox=riscv64 sandbox=normal assets=local"


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    values = parser.parse_args(arguments)
    try:
        print(check_root(values.root))
    except (CheckFailure, OSError, UnicodeError) as error:
        print(f"FIREFOX_M5_ROOTFS_FAIL reason={error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
