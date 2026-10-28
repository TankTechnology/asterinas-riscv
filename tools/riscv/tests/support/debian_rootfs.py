#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import copy
import hashlib
import json
import os
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest import mock

from tools.riscv.debian.rootfs import fsops as fsops_module
from tools.riscv.debian.rootfs import desktop_input_identity
from tools.riscv.debian.rootfs import gate_runtime as gate_runtime_module
from tools.riscv.debian.rootfs import rootfs_gate as rootfs_gate_module
from tools.riscv.debian.rootfs import rootfs_gate_backend as gate_backend_module
from tools.riscv.debian.rootfs.contract import (
    ContractError,
    GATE_IDENTITY_PACKAGES,
    INSTALL_PACKAGES,
    ROOT_LABEL,
    load_manifest,
    parse_packages_lock,
    validate_frozen_root,
)
from tools.riscv.debian.rootfs.desktop_m3_gate import (
    DESKTOP_M3_BOOTARGS,
    DESKTOP_M3_MILESTONES,
    DesktopM3Operations,
    capture_rendered_ppm,
    classify_desktop_m3,
    desktop_m3_qemu_argv,
    inspect_ppm,
)
from tools.riscv.debian.rootfs.desktop_m4_gate import (
    DESKTOP_M4_MILESTONES,
    DesktopM4Operations,
    classify_desktop_m4,
    desktop_m4_qemu_argv,
)
from tools.riscv.debian.rootfs.gate_protocol import (
    MAX_COMMAND_PAYLOAD_BYTES,
    MAX_TRANSCRIPT_BYTES,
    BootEvidence,
    GateResult,
    ShellCommand,
    classify_boot,
    qemu_argv,
    shell_commands,
    classify_systemd_m2,
)
from tools.riscv.debian.rootfs.gate_runtime import (
    EarlyProcessExit,
    GateTermination,
    HmpMonitor,
    MonitorError,
    PinnedOutputDirectory,
    SerialConsole,
    TerminationSignalState,
    launch_process,
    teardown_gate,
)
from tools.riscv.debian.rootfs.rootfs_gate import (
    GateConfig,
    GateFailure,
    build_boot_image,
    copy_sparse_root,
    orchestrate_gate,
    parse_gate_args,
    verify_four_hart_dtb,
)
from tools.riscv.debian.rootfs.profiles import get_profile
from tools.riscv.debian.rootfs.systemd_m2_gate import (
    SYSTEMD_EXT4_M3_BOOTARGS,
    SYSTEMD_M2_BOOTARGS,
    SystemdM2Operations,
    orchestrate_systemd_m2_gate,
    systemd_m2_bootargs,
    systemd_m2_qemu_argv,
)


ROOT_IMAGE_SIZE_BYTES = 1024 * 1024 * 1024
ZERO_FILLED_ROOT_SHA256 = (
    "49bc20df15e412a64472421e13fe86ff1c5165e18b2afccf160d4dc19fe68a14"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
BUILD_SCRIPT = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/build_rootfs.sh"
SYSTEMD_M2_EVIDENCE_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/systemd_m2_evidence.sh"
)
SHUTDOWN_SYNC_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/shutdown_sync.sh"
)
DESKTOP_M3_EVIDENCE_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m3_evidence.sh"
)
DESKTOP_M3_SESSION_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m3_session.sh"
)
DESKTOP_INPUT_IDENTITY_SOURCE = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_input_identity.py"
)
DESKTOP_M4_EVIDENCE_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m4_evidence.sh"
)
DESKTOP_M4_SESSION_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m4_session.sh"
)
DESKTOP_M4_X11_PAINT_SCRIPT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m4_x11_paint.py"
)
DESKTOP_M4_WELCOME_PAGE = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_m4_welcome.html"
)
DESKTOP_WALLPAPER = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_wallpaper.svg"
DESKTOP_ANIME_WALLPAPER = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_anime_wallpaper.png"
)
DESKTOP_PCMANFM_CONFIG = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_pcmanfm.conf"
)
DESKTOP_LXPANEL_CONFIG = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_lxpanel.conf"
)
STAGE1_BUILD_SCRIPT = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/build_stage1.sh"
STAGE1_SOURCE = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/stage1_init.c"
STAGE1_DEBUG_CONSOLE_SOURCE = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/stage1_debug_console.c"
)
STAGE1_SAFE_REBOOT_SOURCE = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/megrez_safe_reboot.sh"
)
STAGE1_PROBE_SOURCE = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/stage1_probe.c"
STAGE1_BROWSER_GATE = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/browser_web_marionette_gate.py"
)
STAGE1_BROWSER_INTERACTION_PERF = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/browser_interaction_perf.py"
)
STAGE1_BROWSER_SYSTEM_TIME = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/browser_system_time.py"
)
STAGE1_BROWSER_LATENCY_CONTRACT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/browser_latency_contract.py"
)
STAGE1_BROWSER_DAILY_USE_CONTRACT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/browser_daily_use_contract.py"
)
STAGE1_BROWSER_PERF_CAPTURE = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/browser_perf_capture.py"
)
STAGE1_BROWSER_WORKLOAD_CONTRACT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/browser_workload_contract.py"
)
STAGE1_BROWSER_COMPOSITE_CAPTURE = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/browser_composite_capture.py"
)
STAGE1_BROWSER_MARIONETTE_TRANSPORT = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/browser_marionette_transport.py"
)
STAGE1_BROWSER_DAILY_USE_GATE = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/browser_daily_use_gate.py"
)
STAGE1_BROWSER_DAILY_USE_UPLOAD = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/browser_daily_use_upload.py"
)
STAGE1_CLOCK_SYNC = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/megrez_clock_sync.py"
STAGE1_PHYSICAL_EXTERNAL_SERVICES_QUIESCE = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/physical_external_services_quiesce.sh"
)
STAGE1_PHYSICAL_GRAPHICS_GATE = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/physical_graphics_gate.py"
)
STAGE1_PHYSICAL_GRAPHICS_CONTROL = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/physical_graphics_control.sh"
)
STAGE1_DESKTOP_INPUT_IDENTITY = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/desktop_input_identity.py"
)
STAGE1_PHYSICAL_GRAPHICS_PAGE = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/physical_graphics_interaction.html"
)
STAGE1_PHYSICAL_SYSTEM_PROBE = (
    REPOSITORY_ROOT / "tools/riscv/debian/rootfs/physical_system_probe.sh"
)
STAGE1_DEBUG_CONSOLE_INCLUDE = STAGE1_DEBUG_CONSOLE_SOURCE.parent
CONTRACT_MODULE = "tools.riscv.debian.rootfs.contract"
REQUIRED_TOOLS = (
    "debootstrap",
    "qemu-riscv64-static",
    "gpgv",
    "dpkg-query",
    "mke2fs",
    "dumpe2fs",
    "debugfs",
    "sha256sum",
    "curl",
)
PUBLISHED_ARTIFACTS = (
    "debian-root.ext2",
    "rootfs-manifest.json",
    "packages.lock",
    "source-metadata/InRelease",
    "source-metadata/package-checksums",
)

PACKAGE_ROWS = (
    ("base-files", "riscv64", "13.8+deb13u1"),
    ("bash", "riscv64", "5.2.37-2+b5"),
    ("ca-certificates", "all", "20250419"),
    ("coreutils", "riscv64", "9.7-3"),
    ("libc6", "riscv64", "2.41-12"),
    ("procps", "riscv64", "2:4.0.4-9"),
    ("util-linux", "riscv64", "2.41-5"),
)

SYSTEMD_M2_PACKAGE_ROWS = tuple(
    sorted(
        PACKAGE_ROWS
        + (
            ("dbus", "riscv64", "1.16.2-2"),
            ("systemd", "riscv64", "257.8-1"),
            ("systemd-sysv", "riscv64", "257.8-1"),
        )
    )
)


def _lock_text(rows: tuple[tuple[str, str, str], ...] = PACKAGE_ROWS) -> str:
    return "".join("\t".join(row) + "\n" for row in rows)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _parse_newc_entries(
    archive: bytes,
) -> list[tuple[str, int, int, int, int, bytes]]:
    entries = []
    offset = 0
    while True:
        header = archive[offset : offset + 110]
        if len(header) != 110 or header[:6] != b"070701":
            raise ValueError("invalid raw newc header")
        fields = tuple(
            int(header[field_offset : field_offset + 8], 16)
            for field_offset in range(6, 110, 8)
        )
        mode, uid, gid = fields[1:4]
        mtime = fields[5]
        file_size = fields[6]
        name_size = fields[11]
        name_start = offset + 110
        name_end = name_start + name_size
        if archive[name_end - 1 : name_end] != b"\0":
            raise ValueError("newc entry name is not terminated")
        name = archive[name_start : name_end - 1].decode("ascii")
        data_start = (name_end + 3) & ~3
        data_end = data_start + file_size
        offset = (data_end + 3) & ~3
        if name == "TRAILER!!!":
            if any(archive[offset:]):
                raise ValueError("nonzero bytes after newc trailer")
            return entries
        entries.append((name, mode, uid, gid, mtime, archive[data_start:data_end]))


def _manifest_payload(packages_lock_sha256: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "suite": "trixie",
        "debian_release": "13.6",
        "mirror_url": "https://deb.debian.org/debian",
        "architecture": "riscv64",
        "signed_metadata": {
            "url": "https://deb.debian.org/debian/dists/trixie/InRelease",
            "sha256": hashlib.sha256(b"InRelease").hexdigest(),
        },
        "packages_lock_sha256": packages_lock_sha256,
        "downloaded_packages": [
            {
                "name": name,
                "architecture": architecture,
                "version": version,
                "sha256": hashlib.sha256(name.encode()).hexdigest(),
            }
            for name, architecture, version in PACKAGE_ROWS
        ],
        "filesystem": {
            "type": "ext2",
            "label": ROOT_LABEL,
            "uuid": "7b7ad749-77d0-4e59-89e4-e117244a70aa",
            "size_bytes": ROOT_IMAGE_SIZE_BYTES,
            "block_size_bytes": 4096,
        },
        "tool_versions": {
            "debootstrap": "1.0.141",
            "mke2fs": "1.47.2",
            "qemu-riscv64-static": "10.0.2",
        },
        "build_timestamp": "2026-08-24T00:00:00Z",
        "root_image_sha256": ZERO_FILLED_ROOT_SHA256,
        "gate_packages": {
            name: version
            for name, architecture, version in PACKAGE_ROWS
            if name in GATE_IDENTITY_PACKAGES and architecture == "riscv64"
        },
    }


def _run_builder(
    *arguments: str,
    cwd: Path,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(BUILD_SCRIPT), *arguments],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_builder_function(
    function: str,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            'source "$1"; shift; "$@"',
            "builder-function-test",
            str(BUILD_SCRIPT),
            function,
            *arguments,
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_image_creation(
    work_directory: Path,
    *,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            'source "$1"; PROFILE=minimal; configure_profile 0; '
            'WORK_DIR="$2"; create_and_verify_image',
            "builder-image-test",
            str(BUILD_SCRIPT),
            str(work_directory),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_publish_artifacts(
    work_directory: Path,
    output_directory: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            'source "$1"; WORK_DIR="$2"; OUTPUT_DIR="$3"; publish_artifacts',
            "builder-publish-test",
            str(BUILD_SCRIPT),
            str(work_directory),
            str(output_directory),
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_prepare_private_workspace(
    output_directory: Path,
    cache_directory: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            """source "$1"
OUTPUT_DIR="$2"
CACHE_DIR="$3"
prepare_private_workspace
stat -c 'PRIVATE_MODE=%a' "$WORK_DIR"
""",
            "builder-workspace-test",
            str(BUILD_SCRIPT),
            str(output_directory),
            str(cache_directory),
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_admit_downloaded_packages(
    work_directory: Path,
    cache_directory: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            'source "$1"; WORK_DIR="$2"; CACHE_DIR="$3"; admit_downloaded_packages',
            "builder-cache-test",
            str(BUILD_SCRIPT),
            str(work_directory),
            str(cache_directory),
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _make_test_deb(
    path: Path,
    *,
    package: str,
    version: str,
    architecture: str = "riscv64",
) -> None:
    package_root = path.parent / f"{path.name}.root"
    control_directory = package_root / "DEBIAN"
    control_directory.mkdir(parents=True)
    (control_directory / "control").write_text(
        f"Package: {package}\n"
        f"Version: {version}\n"
        f"Architecture: {architecture}\n"
        "Maintainer: Test Fixture <fixture@example.com>\n"
        "Description: package admission test fixture\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["dpkg-deb", "--build", "--root-owner-group", str(package_root), str(path)],
        check=True,
        capture_output=True,
        text=True,
    )


def _make_fake_tools(directory: Path, *, failing_tool: str | None = None) -> Path:
    bin_directory = directory / "fake-bin"
    bin_directory.mkdir()
    for tool in REQUIRED_TOOLS:
        tool_path = bin_directory / tool
        exit_status = 97 if tool == failing_tool else 0
        tool_path.write_text(
            f"#!/bin/sh\nexit {exit_status}\n",
            encoding="utf-8",
        )
        tool_path.chmod(0o755)
    # Profile lookup is part of the builder's normal startup path, including
    # minimal; keep a real interpreter available even when the other tools
    # are deliberately fake.
    python = bin_directory / "python3"
    python.write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    dirname = bin_directory / "dirname"
    dirname.write_text("#!/bin/sh\nexec /usr/bin/dirname \"$@\"\n", encoding="utf-8")
    dirname.chmod(0o755)
    return bin_directory


def _make_fake_root_stat(directory: Path) -> Path:
    bin_directory = directory / "fake-stat-bin"
    bin_directory.mkdir()
    stat = bin_directory / "stat"
    stat.write_text(
        """#!/bin/sh
if [ "$1" = "-c" ] && [ "$2" = "%u %a" ]; then
    shift 2
    [ "$1" != "--" ] || shift
    owner=0
    case "$1" in
        *nonroot*) owner=1000 ;;
    esac
    mode=$(/usr/bin/stat -c %a -- "$1") || exit
    printf '%s %s\n' "$owner" "$mode"
    exit 0
fi
exec /usr/bin/stat "$@"
""",
        encoding="utf-8",
    )
    stat.chmod(0o755)
    return bin_directory


def _package_checksums_text(
    package_rows: tuple[tuple[str, str, str], ...] = PACKAGE_ROWS,
) -> str:
    rows = [(*row, hashlib.sha256(row[0].encode()).hexdigest()) for row in package_rows]
    return "".join("\t".join(row) + "\n" for row in sorted(rows))


def _run_configure_rootfs(
    work_directory: Path,
    profile: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            """source "$1"
PROFILE="$3"
configure_profile 1
WORK_DIR="$2"
configure_and_normalize_rootfs
""",
            "builder-configure-test",
            str(BUILD_SCRIPT),
            str(work_directory),
            profile,
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

__all__ = tuple(
    name
    for name in globals()
    if name not in {"annotations"} and not name.startswith("__")
)
