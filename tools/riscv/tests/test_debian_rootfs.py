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
from tools.riscv.debian.rootfs.gate_protocol import (
    MAX_COMMAND_PAYLOAD_BYTES,
    MAX_TRANSCRIPT_BYTES,
    BootEvidence,
    GateResult,
    ShellCommand,
    classify_boot,
    qemu_argv,
    shell_commands,
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


ROOT_IMAGE_SIZE_BYTES = 1024 * 1024 * 1024
ZERO_FILLED_ROOT_SHA256 = (
    "49bc20df15e412a64472421e13fe86ff1c5165e18b2afccf160d4dc19fe68a14"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BUILD_SCRIPT = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/build_rootfs.sh"
STAGE1_BUILD_SCRIPT = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/build_stage1.sh"
STAGE1_SOURCE = REPOSITORY_ROOT / "tools/riscv/debian/rootfs/stage1_init.c"
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
            'source "$1"; WORK_DIR="$2"; create_and_verify_image',
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


def _package_checksums_text() -> str:
    rows = [(*row, hashlib.sha256(row[0].encode()).hexdigest()) for row in PACKAGE_ROWS]
    return "".join("\t".join(row) + "\n" for row in sorted(rows))


class DebianStage1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def make_stage1_publication_sources(self) -> tuple[Path, Path]:
        source_directory = self.directory / "stage1 publication sources"
        source_directory.mkdir(exist_ok=True)
        init_source = source_directory / "init"
        archive_source = source_directory / "initramfs.cpio"
        init_source.write_bytes(b"new-stage1-init")
        archive_source.write_bytes(b"new-stage1-archive")
        return init_source, archive_source

    def stage1_publication_snapshot(
        self, output_directory: Path
    ) -> dict[str, tuple[bytes, int] | None]:
        snapshot = {}
        for name in ("init", "initramfs.cpio"):
            path = output_directory / name
            snapshot[name] = (
                (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                if path.exists()
                else None
            )
        return snapshot

    def wait_for_child(self, process_id: int) -> int:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            waited_id, status = os.waitpid(process_id, os.WNOHANG)
            if waited_id == process_id:
                return status
            time.sleep(0.01)
        os.kill(process_id, signal.SIGKILL)
        os.waitpid(process_id, 0)
        self.fail("stage1 publication child did not exit within two seconds")

    def compile_stage1(
        self,
        output: Path,
        define: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "cc",
            "-std=c11",
            "-O2",
            "-static",
            "-Wall",
            "-Wextra",
            "-Werror",
        ]
        if define is not None:
            command.append(f"-D{define}")
        command.extend((str(STAGE1_SOURCE), "-o", str(output)))
        return subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def run_builder(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
        cwd: Path = REPOSITORY_ROOT,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(STAGE1_BUILD_SCRIPT), *arguments],
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_native_self_test_covers_discovery_and_handoff_failures(self) -> None:
        binary = self.directory / "stage1-self-test"
        compilation = self.compile_stage1(binary, "DEBIAN_STAGE1_SELF_TEST")
        self.assertEqual(compilation.returncode, 0, compilation.stderr)
        cases = (
            "one-valid-device",
            "no-match",
            "two-matching-devices",
            "bad-ext2-magic",
            "wrong-label",
            "non-block-device",
            "delayed-valid-device",
            "device-before-deadline",
            "device-at-deadline",
            "device-after-deadline",
            "root-mount-failure",
            "dev-bind-failure",
            "proc-mount-failure",
            "sysfs-mount-failure",
            "run-mount-failure",
            "tmp-mount-failure",
            "chroot-failure",
            "chdir-failure",
            "exec-failure",
            "discovery-deadline",
        )

        for case in cases:
            with self.subTest(case=case):
                result = subprocess.run(
                    [binary, case],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout,
                    f"DEBIAN_STAGE1_SELF_TEST PASS case={case}\n",
                )

    def test_normal_failure_lifecycle_flushes_one_marker_and_holds(self) -> None:
        binary = self.directory / "stage1-lifecycle-test"
        compilation = self.compile_stage1(binary, "DEBIAN_STAGE1_LIFECYCLE_TEST")
        self.assertEqual(compilation.returncode, 0, compilation.stderr)

        with subprocess.Popen(
            [binary],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            try:
                self.assertIsNotNone(process.stdout)
                readable, _, _ = select.select([process.stdout], [], [], 2.0)
                self.assertTrue(readable, "stage1 did not flush its failure marker")
                self.assertEqual(
                    process.stdout.readline(),
                    "DEBIAN_ROOTFS_FAIL reason=test-lifecycle\n",
                )
                readable, _, _ = select.select([process.stdout], [], [], 0.1)
                self.assertFalse(readable, "stage1 printed more than one marker")
                self.assertIsNone(
                    process.poll(), "stage1 exited after terminal failure"
                )
            finally:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=2)

    def test_console_duplication_clears_cloexec_for_same_number_fd(self) -> None:
        harness_source = self.directory / "console-fd-harness.c"
        harness_source.write_text(
            f'''#define main stage1_production_main
#include "{STAGE1_SOURCE}"
#undef main

int main(int argc, char **argv)
{{
    if (argc != 2 || argv[1][0] < '0' || argv[1][0] > '2' || argv[1][1] != '\\0')
        return 2;
    int target_fd = argv[1][0] - '0';
    if (close(target_fd) != 0)
        return 3;
    int console_fd = open("/dev/null", O_RDWR | O_CLOEXEC);
    if (console_fd != target_fd)
        return 4;
    if (duplicate_console_fd(console_fd, target_fd) < 0)
        return 5;
    int flags = fcntl(target_fd, F_GETFD);
    if (flags < 0)
        return 6;
    return (flags & FD_CLOEXEC) != 0;
}}
''',
            encoding="utf-8",
        )
        binary = self.directory / "console-fd-harness"
        compilation = subprocess.run(
            [
                "cc",
                "-std=c11",
                "-O2",
                "-static",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-Wno-return-type",
                harness_source,
                "-o",
                binary,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(compilation.returncode, 0, compilation.stderr)

        for target_fd in range(3):
            with self.subTest(target_fd=target_fd):
                result = subprocess.run([binary, str(target_fd)], check=False)
                self.assertEqual(result.returncode, 0)

    def test_builder_declares_exact_tools_and_entries(self) -> None:
        tools = self.run_builder("--print-tools")
        entries = self.run_builder("--print-entries")

        self.assertEqual(tools.returncode, 0, tools.stderr)
        self.assertEqual(
            tools.stdout.splitlines(),
            ["riscv64-linux-gnu-gcc", "cpio", "python3"],
        )
        self.assertEqual(entries.returncode, 0, entries.stderr)
        self.assertEqual(entries.stdout.splitlines(), [".", "init"])

    def test_builder_rejects_unknown_arguments(self) -> None:
        result = self.run_builder("--unknown")

        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown option", result.stderr)

    def test_builder_is_deterministic_with_exact_raw_newc_metadata(self) -> None:
        environment = os.environ.copy()
        environment["RISC_V_CC"] = "cc"
        environment["SOURCE_DATE_EPOCH"] = "1700000000"
        first = self.directory / "first output" / "initramfs.cpio"
        second = self.directory / "second output" / "initramfs.cpio"

        for output in (first, second):
            result = self.run_builder(
                str(output),
                environment=environment,
                cwd=self.directory,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            time.sleep(1.1)

        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((first.parent / "init").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((second.parent / "init").stat().st_mode), 0o755)
        entries = _parse_newc_entries(first.read_bytes())
        self.assertEqual(
            [
                (name, mode, uid, gid, mtime)
                for name, mode, uid, gid, mtime, _ in entries
            ],
            [
                (".", stat.S_IFDIR | 0o755, 0, 0, 1700000000),
                ("init", stat.S_IFREG | 0o755, 0, 0, 1700000000),
            ],
        )
        self.assertTrue(entries[1][5].startswith(b"\x7fELF"))
        self.assertEqual(entries[1][5], (first.parent / "init").read_bytes())

    def test_builder_rejects_invalid_source_date_epoch(self) -> None:
        for value in ("", "00", "01", "+1", "-1", "1.0", "4294967296"):
            with self.subTest(value=value):
                environment = os.environ.copy()
                environment["RISC_V_CC"] = "cc"
                environment["SOURCE_DATE_EPOCH"] = value
                output = self.directory / f"invalid-{value.replace('/', '_')}.cpio"

                result = self.run_builder(str(output), environment=environment)

                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("SOURCE_DATE_EPOCH", result.stderr)
                self.assertFalse(output.exists())

    def test_builder_rejects_directory_destination_without_mutation(self) -> None:
        environment = os.environ.copy()
        environment["RISC_V_CC"] = "cc"
        destination = self.directory / "archive.cpio"
        destination.mkdir()
        sentinel = destination / "sentinel"
        sentinel.write_bytes(b"preserve directory")

        result = self.run_builder(str(destination), environment=environment)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unsafe output path", result.stderr)
        self.assertEqual(list(destination.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"preserve directory")

    def test_builder_failure_preserves_existing_archive(self) -> None:
        failing_compiler = self.directory / "failing-compiler"
        failing_compiler.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
        failing_compiler.chmod(0o755)
        destination = self.directory / "existing archive.cpio"
        destination.write_bytes(b"known-good-archive")
        destination.chmod(0o640)
        existing_init = destination.parent / "init"
        existing_init.write_bytes(b"known-good-init")
        existing_init.chmod(0o750)
        environment = os.environ.copy()
        environment["RISC_V_CC"] = str(failing_compiler)

        result = self.run_builder(str(destination), environment=environment)

        self.assertEqual(result.returncode, 97, result.stderr)
        self.assertEqual(destination.read_bytes(), b"known-good-archive")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o640)
        self.assertEqual(existing_init.read_bytes(), b"known-good-init")
        self.assertEqual(stat.S_IMODE(existing_init.stat().st_mode), 0o750)

    def test_builder_publication_failure_preserves_existing_pair(self) -> None:
        fake_bin = self.directory / "publication-failure-bin"
        fake_bin.mkdir()
        fake_python = fake_bin / "python3"
        fake_python.write_text("#!/bin/sh\nexit 96\n", encoding="utf-8")
        fake_python.chmod(0o755)
        output = self.directory / "publication failure" / "initramfs.cpio"
        output.parent.mkdir()
        output.write_bytes(b"old-archive")
        output.chmod(0o640)
        init_output = output.parent / "init"
        init_output.write_bytes(b"old-init")
        init_output.chmod(0o750)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
        environment["RISC_V_CC"] = "cc"

        result = self.run_builder(str(output), environment=environment)

        self.assertEqual(result.returncode, 96, result.stderr)
        self.assertEqual(output.read_bytes(), b"old-archive")
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o640)
        self.assertEqual(init_output.read_bytes(), b"old-init")
        self.assertEqual(stat.S_IMODE(init_output.stat().st_mode), 0o750)

    def test_stage1_publication_rolls_back_second_replace_failure(self) -> None:
        init_source, archive_source = self.make_stage1_publication_sources()
        real_replace = os.replace

        for had_existing_pair in (False, True):
            with self.subTest(had_existing_pair=had_existing_pair):
                output = self.directory / f"replace-failure-{had_existing_pair}"
                output.mkdir(mode=0o710)
                if had_existing_pair:
                    (output / "init").write_bytes(b"old-init")
                    (output / "init").chmod(0o751)
                    (output / "initramfs.cpio").write_bytes(b"old-archive")
                    (output / "initramfs.cpio").chmod(0o640)
                original = self.stage1_publication_snapshot(output)
                replace_count = 0

                def fail_second_replace(*args, **kwargs):
                    nonlocal replace_count
                    replace_count += 1
                    if replace_count == 2:
                        raise OSError("injected stage1 second replace failure")
                    return real_replace(*args, **kwargs)

                with (
                    mock.patch.object(
                        fsops_module.os,
                        "replace",
                        new=fail_second_replace,
                    ),
                    self.assertRaisesRegex(OSError, "injected stage1 second replace"),
                ):
                    fsops_module.publish_stage1(
                        output,
                        init_source,
                        archive_source,
                    )

                self.assertEqual(self.stage1_publication_snapshot(output), original)
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o710)
                self.assertEqual(
                    sorted(path.name for path in output.iterdir()),
                    sorted(
                        name for name, value in original.items() if value is not None
                    ),
                )

    def test_stage1_publication_rolls_back_hup_and_term(self) -> None:
        init_source, archive_source = self.make_stage1_publication_sources()

        for signum in (signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signum=signum):
                output = self.directory / f"signal-{signum}"
                output.mkdir(mode=0o711)
                (output / "init").write_bytes(b"old-init")
                (output / "init").chmod(0o750)
                (output / "initramfs.cpio").write_bytes(b"old-archive")
                (output / "initramfs.cpio").chmod(0o640)
                original = self.stage1_publication_snapshot(output)
                process_id = os.fork()
                if process_id == 0:
                    real_replace = os.replace
                    replace_count = 0

                    def signal_second_replace(*args, **kwargs):
                        nonlocal replace_count
                        replace_count += 1
                        if replace_count == 2:
                            os.kill(os.getpid(), signum)
                        return real_replace(*args, **kwargs)

                    try:
                        with mock.patch.object(
                            fsops_module.os,
                            "replace",
                            new=signal_second_replace,
                        ):
                            fsops_module.publish_stage1(
                                output,
                                init_source,
                                archive_source,
                            )
                    except fsops_module.PublishInterrupted as error:
                        os._exit(128 + error.signum)
                    except BaseException:
                        os._exit(99)
                    os._exit(0)

                wait_status = self.wait_for_child(process_id)
                self.assertTrue(os.WIFEXITED(wait_status))
                self.assertEqual(os.WEXITSTATUS(wait_status), 128 + signum)
                self.assertEqual(self.stage1_publication_snapshot(output), original)
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o711)
                self.assertEqual(
                    sorted(path.name for path in output.iterdir()),
                    ["init", "initramfs.cpio"],
                )

    def test_stage1_publication_defers_post_commit_signal(self) -> None:
        init_source, archive_source = self.make_stage1_publication_sources()

        for signum in (signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signum=signum):
                output = self.directory / f"post-commit-{signum}"
                output.mkdir(mode=0o711)
                (output / "init").write_bytes(b"old-init")
                (output / "initramfs.cpio").write_bytes(b"old-archive")
                real_cleanup = fsops_module._cleanup_publication_files

                def signal_during_cleanup(entries):
                    os.kill(os.getpid(), signum)
                    real_cleanup(entries)

                with mock.patch.object(
                    fsops_module,
                    "_cleanup_publication_files",
                    new=signal_during_cleanup,
                ):
                    result = fsops_module.main(
                        [
                            "publish-stage1",
                            "--output-dir",
                            str(output),
                            "--init-source",
                            str(init_source),
                            "--archive-source",
                            str(archive_source),
                        ]
                    )

                self.assertEqual(result, 128 + signum)
                self.assertEqual(
                    self.stage1_publication_snapshot(output),
                    {
                        "init": (b"new-stage1-init", 0o755),
                        "initramfs.cpio": (b"new-stage1-archive", 0o644),
                    },
                )
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o711)


class DebianRootfsBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def test_prints_exact_required_tool_contract(self) -> None:
        result = _run_builder("--print-tools", cwd=self.directory)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), list(REQUIRED_TOOLS))
        self.assertEqual(result.stderr, "")

    def test_prints_exact_explicit_package_contract(self) -> None:
        result = _run_builder("--print-packages", cwd=self.directory)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), list(INSTALL_PACKAGES))
        self.assertEqual(result.stderr, "")

    def test_rejects_unknown_argument(self) -> None:
        result = _run_builder("--not-an-option", cwd=self.directory)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown argument", result.stderr)

    def test_rejects_non_https_mirror(self) -> None:
        result = _run_builder(
            "--mirror",
            "http://deb.debian.org/debian",
            cwd=self.directory,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HTTPS", result.stderr)

    def test_rejects_unsupported_suite(self) -> None:
        result = _run_builder("--suite", "bookworm", cwd=self.directory)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported suite", result.stderr)

    def test_rejects_missing_required_tool(self) -> None:
        bin_directory = _make_fake_tools(self.directory)
        (bin_directory / "mke2fs").unlink()
        environment = os.environ.copy()
        environment["PATH"] = str(bin_directory)

        result = _run_builder(cwd=self.directory, environment=environment)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing required tool: mke2fs", result.stderr)

    def test_rejects_unsafe_output_and_cache_paths(self) -> None:
        real_directory = self.directory / "real"
        real_directory.mkdir()
        symlink_path = self.directory / "symlink"
        symlink_path.symlink_to(real_directory, target_is_directory=True)
        masked_symlink_path = self.directory / "missing" / ".." / "symlink"
        cases = (
            ("--output-dir", str(symlink_path)),
            ("--cache-dir", str(symlink_path)),
            ("--output-dir", str(masked_symlink_path)),
            ("--cache-dir", str(masked_symlink_path)),
            (
                "--output-dir",
                str(real_directory),
                "--cache-dir",
                str(real_directory),
            ),
            (
                "--output-dir",
                str(real_directory),
                "--cache-dir",
                str(symlink_path / "cache"),
            ),
        )

        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = _run_builder(*arguments, cwd=self.directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsafe", result.stderr)

    def test_rejects_symlinked_publication_directory(self) -> None:
        output_directory = self.directory / "output"
        metadata_directory = self.directory / "metadata"
        output_directory.mkdir()
        metadata_directory.mkdir()
        (output_directory / "source-metadata").symlink_to(
            metadata_directory,
            target_is_directory=True,
        )

        result = _run_builder(
            "--output-dir",
            str(output_directory),
            "--cache-dir",
            str(self.directory / "cache"),
            cwd=self.directory,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe", result.stderr)

    def test_rejects_symlinked_content_cache_directory(self) -> None:
        cache_directory = self.directory / "cache"
        cache_target = self.directory / "cache-target"
        cache_directory.mkdir()
        cache_target.mkdir()
        (cache_directory / "sha256").symlink_to(
            cache_target,
            target_is_directory=True,
        )

        result = _run_builder(
            "--output-dir",
            str(self.directory / "output"),
            "--cache-dir",
            str(cache_directory),
            cwd=self.directory,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe", result.stderr)

    def test_cache_admission_rejects_symlinked_digest_prefix(self) -> None:
        work_directory = self.directory / "cache-work"
        debs_directory = work_directory / "debs"
        metadata_directory = work_directory / "source-metadata"
        debs_directory.mkdir(parents=True)
        metadata_directory.mkdir()
        archive = debs_directory / "package.deb"
        archive.write_bytes(b"signed package archive")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (work_directory / "package-index-checksums").write_text(
            f"package\triscv64\t1.0\t{digest}\n",
            encoding="utf-8",
        )
        cache_directory = self.directory / "cache"
        sha256_directory = cache_directory / "sha256"
        outside_directory = self.directory / "outside"
        sha256_directory.mkdir(parents=True)
        outside_directory.mkdir()
        (sha256_directory / digest[:2]).symlink_to(
            outside_directory,
            target_is_directory=True,
        )

        result = _run_admit_downloaded_packages(work_directory, cache_directory)

        self.assertFalse((outside_directory / f"{digest}.deb").exists())
        self.assertNotEqual(result.returncode, 0)

    def test_rejects_invalid_source_date_epoch(self) -> None:
        for value in ("", "00", "01", "+1", "-1", "1.0", "4294967296"):
            with self.subTest(value=value):
                environment = os.environ.copy()
                environment["SOURCE_DATE_EPOCH"] = value
                result = _run_builder(cwd=self.directory, environment=environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SOURCE_DATE_EPOCH", result.stderr)

    def test_rejects_packages_index_not_bound_to_retained_inrelease(self) -> None:
        work_directory = self.directory / "work"
        metadata_directory = work_directory / "source-metadata"
        metadata_directory.mkdir(parents=True)
        (metadata_directory / "InRelease").write_text(
            """Codename: trixie
Version: 13.6
SHA256:
 0000000000000000000000000000000000000000000000000000000000000000 12 main/binary-riscv64/Packages
""",
            encoding="utf-8",
        )
        package_index = work_directory / "Packages"
        package_index.write_bytes(b"Package: bash\n")
        result = _run_builder_function(
            "authenticate_package_index",
            str(package_index),
            "main/binary-riscv64/Packages",
            str(metadata_directory / "InRelease"),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not authenticated by retained InRelease", result.stderr)

    def test_rejects_release_drift_after_package_install(self) -> None:
        work_directory = self.directory / "work"
        metadata_directory = work_directory / "source-metadata"
        metadata_directory.mkdir(parents=True)
        (metadata_directory / "InRelease").write_text(
            "Codename: trixie\nVersion: 13.6\n",
            encoding="utf-8",
        )
        bin_directory = self.directory / "release-tools"
        bin_directory.mkdir()
        curl = bin_directory / "curl"
        curl.write_text(
            """#!/bin/sh
while [ "$#" -gt 0 ]; do
    if [ "$1" = "--output" ]; then
        shift
        printf 'Codename: trixie\nVersion: 13.7\n' >"$1"
        exit 0
    fi
    shift
done
exit 64
""",
            encoding="utf-8",
        )
        curl.chmod(0o755)
        gpgv = bin_directory / "gpgv"
        gpgv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gpgv.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{bin_directory}:{environment['PATH']}"

        result = _run_builder_function(
            "verify_release_is_unchanged",
            str(work_directory),
            "https://deb.debian.org/debian",
            "trixie",
            "13.6",
            environment=environment,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("signed release changed during build", result.stderr)

    def test_accepts_safe_packaged_keyring_paths(self) -> None:
        keyring_directory = self.directory / "keyrings"
        keyring_directory.mkdir()
        regular_keyring = keyring_directory / "archive.pgp"
        regular_keyring.write_bytes(b"keyring")
        regular_keyring.chmod(0o644)
        packaged_link = keyring_directory / "archive.gpg"
        packaged_link.symlink_to(regular_keyring.name)
        fake_bin = _make_fake_root_stat(self.directory)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

        for keyring in (regular_keyring, packaged_link):
            with self.subTest(keyring=keyring):
                result = _run_builder_function(
                    "require_safe_keyring_path",
                    str(keyring),
                    environment=environment,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_unsafe_keyring_paths(self) -> None:
        keyring_directory = self.directory / "unsafe-keyrings"
        keyring_directory.mkdir()

        safe_target = keyring_directory / "safe-target.pgp"
        safe_target.write_bytes(b"keyring")
        safe_target.chmod(0o644)
        nested_directory = keyring_directory / "nested"
        nested_directory.mkdir()
        nested_target = nested_directory / "target.pgp"
        nested_target.write_bytes(b"keyring")
        writable_target = keyring_directory / "writable.pgp"
        writable_target.write_bytes(b"keyring")
        writable_target.chmod(0o664)
        nonroot_target = keyring_directory / "nonroot.pgp"
        nonroot_target.write_bytes(b"keyring")
        directory_target = keyring_directory / "directory-target"
        directory_target.mkdir()
        second_link = keyring_directory / "second-link"
        second_link.symlink_to(safe_target.name)
        control_target = keyring_directory / "control\nname"
        control_target.write_bytes(b"keyring")

        unsafe_paths = []
        for name, target in (
            ("absolute", str(safe_target)),
            ("slash", "nested/target.pgp"),
            ("dotdot", "safe..target.pgp"),
            ("missing-link", "missing-target.pgp"),
            ("directory", directory_target.name),
            ("second-symlink", second_link.name),
            ("writable", writable_target.name),
            ("nonroot", nonroot_target.name),
            ("control", control_target.name),
        ):
            link = keyring_directory / name
            link.symlink_to(target)
            unsafe_paths.append(link)
        unsafe_paths.append(keyring_directory / "missing-regular")

        fake_bin = _make_fake_root_stat(self.directory)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
        for keyring in unsafe_paths:
            with self.subTest(keyring=keyring):
                result = _run_builder_function(
                    "require_safe_keyring_path",
                    str(keyring),
                    environment=environment,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsafe Debian archive keyring", result.stderr)

    def test_image_bash_smoke_uses_executable_debugfs_dump(self) -> None:
        work_directory = self.directory / "image-work"
        (work_directory / "stage").mkdir(parents=True)
        bin_directory = self.directory / "image-tools"
        bin_directory.mkdir()
        fake_tools = {
            "mke2fs": "#!/bin/sh\nexit 0\n",
            "dumpe2fs": """#!/bin/sh
cat <<EOF
Filesystem volume name:   ASTER_DEBIANROOT
Filesystem UUID:          7b7ad749-77d0-4e59-89e4-e117244a70aa
Block size:               4096
Filesystem features:      ext_attr resize_inode dir_index filetype sparse_super large_file
EOF
""",
            "debugfs": """#!/bin/sh
case "$2" in
    "stat /usr/bin/qemu-riscv64-static")
        printf 'File not found by ext2_lookup\n'
        ;;
    "stat "*)
        printf 'Inode: 12   Type: regular\n'
        ;;
    "dump /bin/bash "*)
        destination=${2#dump /bin/bash }
        printf 'fake ELF' >"$destination"
        chmod 0600 "$destination"
        ;;
    *) exit 64 ;;
esac
""",
            "qemu-riscv64-static": """#!/bin/sh
if [ ! -x "$3" ]; then
    exit 86
fi
printf 'QEMU_SMOKE_EXECUTED\n'
""",
        }
        for name, contents in fake_tools.items():
            path = bin_directory / name
            path.write_text(contents, encoding="utf-8")
            path.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{bin_directory}:{environment['PATH']}"

        result = _run_image_creation(work_directory, environment=environment)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("QEMU_SMOKE_EXECUTED", result.stdout)

    def test_publish_permissions_only_open_new_directories(self) -> None:
        work_directory = self.directory / "publish-work"
        (work_directory / "source-metadata").mkdir(parents=True)
        for relative_path in PUBLISHED_ARTIFACTS:
            source = work_directory / relative_path
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(relative_path.encode())

        new_output = self.directory / "new-output"
        result = _run_publish_artifacts(work_directory, new_output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(new_output.stat().st_mode & 0o777, 0o755)
        self.assertEqual(
            (new_output / "source-metadata").stat().st_mode & 0o777,
            0o755,
        )
        for relative_path in PUBLISHED_ARTIFACTS:
            self.assertEqual((new_output / relative_path).stat().st_mode & 0o777, 0o644)

        existing_output = self.directory / "existing-output"
        existing_metadata = existing_output / "source-metadata"
        existing_metadata.mkdir(parents=True)
        existing_output.chmod(0o700)
        existing_metadata.chmod(0o700)
        result = _run_publish_artifacts(work_directory, existing_output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(existing_output.stat().st_mode & 0o777, 0o700)
        self.assertEqual(existing_metadata.stat().st_mode & 0o777, 0o700)

    def test_main_propagates_publication_failure_without_success_log(self) -> None:
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                """source "$1"
parse_arguments() { :; }
validate_configuration() { :; }
require_tools() { :; }
prepare_private_workspace() { :; }
fetch_and_verify_release() { :; }
bootstrap_rootfs() { :; }
install_rootfs_packages() { :; }
audit_packages() { :; }
configure_and_normalize_rootfs() { :; }
create_and_verify_image() { :; }
write_rootfs_manifest() { :; }
publish_artifacts() { return 143; }
main
""",
                "builder-publication-failure-test",
                str(BUILD_SCRIPT),
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 143)
        self.assertNotIn("published signed Debian rootfs", result.stderr)

    def test_workspace_permissions_separate_public_and_private_paths(self) -> None:
        public_root = self.directory / "new-public"
        output_directory = public_root / "nested" / "rootfs"
        cache_root = self.directory / "new-cache"
        cache_directory = cache_root / "nested"

        result = _run_prepare_private_workspace(output_directory, cache_directory)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PRIVATE_MODE=700", result.stdout)
        self.assertEqual(public_root.stat().st_mode & 0o777, 0o755)
        self.assertEqual((public_root / "nested").stat().st_mode & 0o777, 0o755)
        self.assertEqual(cache_root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(cache_directory.stat().st_mode & 0o777, 0o700)

        existing_public_root = self.directory / "existing-public"
        existing_public_root.mkdir()
        existing_public_root.chmod(0o700)
        result = _run_prepare_private_workspace(
            existing_public_root / "nested" / "rootfs",
            self.directory / "second-cache",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(existing_public_root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            (existing_public_root / "nested").stat().st_mode & 0o777,
            0o755,
        )

    def test_command_failure_preserves_every_published_artifact(self) -> None:
        output_directory = self.directory / "output with spaces"
        output_directory.mkdir()
        original_contents: dict[str, bytes] = {}
        for index, artifact in enumerate(PUBLISHED_ARTIFACTS):
            artifact_path = output_directory / artifact
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            contents = f"existing artifact {index}\n".encode()
            artifact_path.write_bytes(contents)
            original_contents[artifact] = contents

        bin_directory = _make_fake_tools(self.directory, failing_tool="curl")
        environment = os.environ.copy()
        environment["PATH"] = f"{bin_directory}:{environment['PATH']}"
        result = _run_builder(
            "--output-dir",
            str(output_directory),
            "--cache-dir",
            str(self.directory / "cache with spaces"),
            cwd=self.directory,
            environment=environment,
        )

        self.assertEqual(result.returncode, 97)
        for artifact, contents in original_contents.items():
            self.assertEqual((output_directory / artifact).read_bytes(), contents)


class DebianRootfsFsOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.source = self.directory / "source"
        (self.source / "source-metadata").mkdir(parents=True)
        for relative_path in PUBLISHED_ARTIFACTS:
            source = self.source / relative_path
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(f"new:{relative_path}".encode())

    def make_existing_output(self, name: str = "output") -> Path:
        output = self.directory / name
        (output / "source-metadata").mkdir(parents=True)
        for relative_path in PUBLISHED_ARTIFACTS[1:]:
            destination = output / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(f"old:{relative_path}".encode())
        return output

    def output_snapshot(self, output: Path) -> dict[str, bytes | None]:
        return {
            relative_path: (
                (output / relative_path).read_bytes()
                if (output / relative_path).exists()
                else None
            )
            for relative_path in PUBLISHED_ARTIFACTS
        }

    def test_cache_admission_reuses_and_rejects_corrupt_entry(self) -> None:
        source = self.directory / "package.deb"
        source.write_bytes(b"package")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        cache = self.directory / "cache"

        fsops_module.admit_cache_entry(cache, source, digest)
        destination = cache / "sha256" / digest[:2] / f"{digest}.deb"
        self.assertEqual(destination.read_bytes(), b"package")
        self.assertEqual(destination.stat().st_mode & 0o777, 0o444)
        fsops_module.admit_cache_entry(cache, source, digest)

        destination.chmod(0o644)
        destination.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            fsops_module.admit_cache_entry(cache, source, digest)

    def test_publish_set_rolls_back_second_replace_failure(self) -> None:
        output = self.make_existing_output()
        original = self.output_snapshot(output)
        real_replace = os.replace
        replace_count = 0

        def fail_second_replace(*args, **kwargs):
            nonlocal replace_count
            replace_count += 1
            if replace_count == 2:
                raise OSError("injected second replace failure")
            return real_replace(*args, **kwargs)

        with (
            mock.patch.object(fsops_module.os, "replace", new=fail_second_replace),
            self.assertRaisesRegex(OSError, "injected second replace failure"),
        ):
            fsops_module.publish_set(output, self.source)

        self.assertEqual(replace_count, 1 + len(PUBLISHED_ARTIFACTS))
        self.assertEqual(self.output_snapshot(output), original)

    def test_publish_set_rolls_back_real_sigterm_on_second_replace(self) -> None:
        output = self.make_existing_output()
        original = self.output_snapshot(output)
        process_id = os.fork()
        if process_id == 0:
            real_replace = os.replace
            replace_count = 0

            def signal_second_replace(*args, **kwargs):
                nonlocal replace_count
                replace_count += 1
                if replace_count == 2:
                    os.kill(os.getpid(), signal.SIGTERM)
                return real_replace(*args, **kwargs)

            try:
                with mock.patch.object(
                    fsops_module.os,
                    "replace",
                    new=signal_second_replace,
                ):
                    fsops_module.publish_set(output, self.source)
            except BaseException as error:
                interrupted = getattr(fsops_module, "PublishInterrupted", ())
                if isinstance(error, interrupted):
                    os._exit(128 + error.signum)
                os._exit(99)
            os._exit(0)

        _, wait_status = os.waitpid(process_id, 0)
        self.assertTrue(os.WIFEXITED(wait_status))
        self.assertEqual(os.WEXITSTATUS(wait_status), 143)
        self.assertEqual(self.output_snapshot(output), original)

    def test_publish_set_defers_sigterm_until_after_commit_cleanup(self) -> None:
        output = self.make_existing_output()
        previous_handler = signal.getsignal(signal.SIGTERM)
        real_cleanup = fsops_module._cleanup_publication_files

        def signal_during_cleanup(entries):
            os.kill(os.getpid(), signal.SIGTERM)
            real_cleanup(entries)

        with (
            mock.patch.object(
                fsops_module,
                "_cleanup_publication_files",
                new=signal_during_cleanup,
            ),
            self.assertRaises(fsops_module.PublishInterrupted) as caught,
        ):
            fsops_module.publish_set(output, self.source)

        self.assertEqual(caught.exception.signum, signal.SIGTERM)
        self.assertEqual(
            self.output_snapshot(output),
            self.output_snapshot(self.source),
        )
        self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)

    def test_publish_cli_returns_sigterm_status_after_commit_cleanup(self) -> None:
        output = self.make_existing_output()
        real_cleanup = fsops_module._cleanup_publication_files

        def signal_during_cleanup(entries):
            os.kill(os.getpid(), signal.SIGTERM)
            real_cleanup(entries)

        with mock.patch.object(
            fsops_module,
            "_cleanup_publication_files",
            new=signal_during_cleanup,
        ):
            result = fsops_module.main(
                [
                    "publish-set",
                    "--output-dir",
                    str(output),
                    "--source-root",
                    str(self.source),
                ]
            )

        self.assertEqual(result, 143)
        self.assertEqual(
            self.output_snapshot(output),
            self.output_snapshot(self.source),
        )

    def test_publish_set_rejects_symlinked_or_swapped_output(self) -> None:
        outside = self.directory / "outside"
        outside.mkdir()
        symlink_output = self.directory / "symlink-output"
        symlink_output.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            fsops_module.publish_set(symlink_output, self.source)
        self.assertEqual(list(outside.iterdir()), [])

        output = self.make_existing_output("swap-output")
        moved_output = self.directory / "moved-output"
        original = self.output_snapshot(output)
        real_replace = os.replace
        replace_count = 0

        def swap_before_first_replace(*args, **kwargs):
            nonlocal replace_count
            replace_count += 1
            if replace_count == 1:
                output.rename(moved_output)
                output.symlink_to(outside, target_is_directory=True)
            return real_replace(*args, **kwargs)

        with (
            mock.patch.object(
                fsops_module.os,
                "replace",
                new=swap_before_first_replace,
            ),
            self.assertRaisesRegex(ValueError, "changed during publication"),
        ):
            fsops_module.publish_set(output, self.source)
        self.assertEqual(self.output_snapshot(moved_output), original)
        self.assertEqual(list(outside.iterdir()), [])


class DebianRootfsManifestWriterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary_directory.cleanup)
        cls.directory = Path(cls.temporary_directory.name)
        cls.image = cls.directory / "writer-root.ext2"
        with cls.image.open("wb") as image_file:
            image_file.truncate(ROOT_IMAGE_SIZE_BYTES)

    def setUp(self) -> None:
        self.packages_lock = self.directory / "writer-packages.lock"
        self.inrelease = self.directory / "writer-InRelease"
        self.package_checksums = self.directory / "writer-package-checksums"
        self.output = self.directory / "writer-manifest.json"
        self.reset_inputs()
        self.output.unlink(missing_ok=True)

    def reset_inputs(self) -> None:
        with self.image.open("wb") as image_file:
            image_file.truncate(ROOT_IMAGE_SIZE_BYTES)
        self.packages_lock.write_text(_lock_text(), encoding="utf-8")
        self.inrelease.write_bytes(b"InRelease")
        self.package_checksums.write_text(
            _package_checksums_text(),
            encoding="utf-8",
        )

    def writer_arguments(self) -> list[str]:
        return [
            "write-manifest",
            "--output",
            str(self.output),
            "--image",
            str(self.image),
            "--packages-lock",
            str(self.packages_lock),
            "--inrelease",
            str(self.inrelease),
            "--package-checksums",
            str(self.package_checksums),
            "--mirror",
            "https://deb.debian.org/debian",
            "--suite",
            "trixie",
            "--debian-release",
            "13.6",
            "--build-timestamp",
            "2026-08-24T00:00:00Z",
            "--tool-version",
            "debootstrap=1.0.141",
            "--tool-version",
            "mke2fs=1.47.2",
            "--tool-version",
            "qemu-riscv64-static=10.0.2",
        ]

    def run_writer(
        self,
        arguments: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                CONTRACT_MODULE,
                *(self.writer_arguments() if arguments is None else arguments),
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def run_verifier(
        self,
        arguments: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        exact_arguments = [
            "verify",
            "--image",
            str(self.image),
            "--manifest",
            str(self.output),
            "--packages-lock",
            str(self.packages_lock),
        ]
        return subprocess.run(
            [
                sys.executable,
                "-m",
                CONTRACT_MODULE,
                *(exact_arguments if arguments is None else arguments),
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def write_verifier_fixture(self) -> None:
        self.output.write_text(
            json.dumps(_manifest_payload(_sha256_text(_lock_text()))),
            encoding="utf-8",
        )

    def test_writes_canonical_exact_manifest_consumable_by_task1_contract(
        self,
    ) -> None:
        result = self.run_writer()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        payload = _manifest_payload(_sha256_text(_lock_text()))
        payload["signed_metadata"]["sha256"] = hashlib.sha256(b"InRelease").hexdigest()
        payload["root_image_sha256"] = ZERO_FILLED_ROOT_SHA256
        expected = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        self.assertEqual(self.output.read_text(encoding="utf-8"), expected)
        self.assertEqual(set(json.loads(expected)), set(_manifest_payload("0" * 64)))

        manifest = load_manifest(self.output)
        validated = validate_frozen_root(self.image, manifest, self.packages_lock)
        self.assertEqual(validated.debian_release, "13.6")
        self.assertEqual(validated.downloaded_packages[0][0], "base-files")

    def test_verify_cli_accepts_exact_plan_command_quietly(self) -> None:
        self.write_verifier_fixture()

        result = self.run_verifier()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_verify_cli_rejects_tampered_artifacts(self) -> None:
        cases = ("image", "manifest", "packages-lock")
        for case in cases:
            with self.subTest(case=case):
                self.reset_inputs()
                self.write_verifier_fixture()
                if case == "image":
                    with self.image.open("r+b") as image_file:
                        image_file.write(b"X")
                elif case == "manifest":
                    payload = _manifest_payload(_sha256_text(_lock_text()))
                    payload["suite"] = "bookworm"
                    self.output.write_text(json.dumps(payload), encoding="utf-8")
                else:
                    self.packages_lock.write_text(
                        _lock_text().replace(
                            "bash\triscv64\t5.2.37-2+b5",
                            "bash\triscv64\t5.2.37-2+b6",
                        ),
                        encoding="utf-8",
                    )

                result = self.run_verifier()

                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("contract: error:", result.stderr)

    def test_verify_cli_rejects_missing_and_unknown_arguments(self) -> None:
        self.write_verifier_fixture()
        exact = [
            "verify",
            "--image",
            str(self.image),
            "--manifest",
            str(self.output),
            "--packages-lock",
            str(self.packages_lock),
        ]
        cases = (
            exact[:-2],
            [*exact, "--unknown", "value"],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_verifier(arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("error:", result.stderr)

    def test_rejects_missing_duplicate_unknown_and_invalid_cli_inputs(self) -> None:
        valid = self.writer_arguments()
        cases = (
            ("missing", [valid[0], *valid[3:]]),
            ("duplicate", [*valid, "--suite", "trixie"]),
            ("unknown", [*valid, "--unknown", "value"]),
            (
                "invalid",
                [
                    (
                        "http://deb.debian.org/debian"
                        if value == "https://deb.debian.org/debian"
                        else value
                    )
                    for value in valid
                ],
            ),
        )

        for name, arguments in cases:
            with self.subTest(name=name):
                result = self.run_writer(arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())

    def test_rejects_invalid_package_checksum_rows(self) -> None:
        valid_row = _package_checksums_text().splitlines()[0]
        cases = (
            f"{valid_row}\n{valid_row}\n",
            "bash\triscv64\t5.2.37-2+b5\tnot-a-hash\n",
            "bash\triscv64\t5.2.37-2+b5\n",
        )

        for contents in cases:
            with self.subTest(contents=contents):
                self.package_checksums.write_text(contents, encoding="utf-8")
                result = self.run_writer()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.output.exists())

    def test_refuses_symlink_output_without_changing_target(self) -> None:
        self.output.symlink_to(self.packages_lock)

        result = self.run_writer()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(self.packages_lock.read_text(encoding="utf-8"), _lock_text())

    def test_refuses_output_equal_to_any_input(self) -> None:
        input_paths = (
            self.image,
            self.packages_lock,
            self.inrelease,
            self.package_checksums,
        )

        for input_path in input_paths:
            with self.subTest(input_path=input_path):
                self.reset_inputs()
                arguments = self.writer_arguments()
                arguments[arguments.index("--output") + 1] = str(input_path)
                result = self.run_writer(arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("alias", result.stderr)

    def test_refuses_hardlink_output_alias(self) -> None:
        self.output.hardlink_to(self.packages_lock)

        result = self.run_writer()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("alias", result.stderr)
        self.assertEqual(self.packages_lock.read_text(encoding="utf-8"), _lock_text())


class DebianRootfsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary_directory.cleanup)
        cls.directory = Path(cls.temporary_directory.name)
        cls.image = cls.directory / "debian-root.ext2"
        with cls.image.open("wb") as image_file:
            image_file.truncate(ROOT_IMAGE_SIZE_BYTES)

    def setUp(self) -> None:
        self.packages_lock = self.directory / "packages.lock"
        self.manifest_path = self.directory / "rootfs-manifest.json"
        self.packages_lock.write_text(_lock_text(), encoding="utf-8")
        self.payload = _manifest_payload(_sha256_text(_lock_text()))

    def write_manifest(self, payload: dict[str, object] | None = None) -> None:
        self.manifest_path.write_text(
            json.dumps(self.payload if payload is None else payload),
            encoding="utf-8",
        )

    def load_and_validate(self):
        self.write_manifest()
        manifest = load_manifest(self.manifest_path)
        rows = parse_packages_lock(self.packages_lock)
        return validate_frozen_root(self.image, manifest, self.packages_lock), rows

    def test_accepts_frozen_manifest_and_complete_package_lock(self) -> None:
        validated, rows = self.load_and_validate()

        self.assertEqual(
            INSTALL_PACKAGES,
            (
                "bash",
                "ca-certificates",
                "coreutils",
                "procps",
                "util-linux",
            ),
        )
        self.assertEqual(
            GATE_IDENTITY_PACKAGES,
            (
                "base-files",
                "libc6",
                "bash",
                "coreutils",
                "util-linux",
            ),
        )
        self.assertEqual(ROOT_LABEL, "ASTER_DEBIANROOT")
        self.assertEqual(rows, PACKAGE_ROWS)
        self.assertEqual(validated.debian_release, "13.6")
        self.assertEqual(validated.filesystem.size_bytes, ROOT_IMAGE_SIZE_BYTES)
        with self.assertRaises(FrozenInstanceError):
            validated.suite = "forky"
        with self.assertRaises(FrozenInstanceError):
            validated.filesystem.label = "mutable"

    def test_root_label_fits_ext2_limit(self) -> None:
        encoded_label = ROOT_LABEL.encode("ascii")

        self.assertLessEqual(len(encoded_label), 16)

    def test_accepts_signed_debian_13_point_release_versions(self) -> None:
        for release in ("13.0", "13.6", "13.10"):
            with self.subTest(release=release):
                self.payload["debian_release"] = release
                self.write_manifest()
                manifest = load_manifest(self.manifest_path)
                validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_missing_and_unknown_json_keys(self) -> None:
        cases: list[tuple[str, dict[str, object]]] = []

        missing_top_level = copy.deepcopy(self.payload)
        del missing_top_level["architecture"]
        cases.append(("missing manifest fields", missing_top_level))

        unknown_top_level = copy.deepcopy(self.payload)
        unknown_top_level["extra"] = "not allowed"
        cases.append(("unknown manifest fields", unknown_top_level))

        missing_filesystem = copy.deepcopy(self.payload)
        del missing_filesystem["filesystem"]["uuid"]
        cases.append(("missing filesystem fields", missing_filesystem))

        unknown_signed_metadata = copy.deepcopy(self.payload)
        unknown_signed_metadata["signed_metadata"]["signature"] = "detached"
        cases.append(("unknown signed_metadata fields", unknown_signed_metadata))

        for expected_error, payload in cases:
            with self.subTest(expected_error=expected_error):
                self.write_manifest(payload)
                with self.assertRaisesRegex(ValueError, expected_error):
                    load_manifest(self.manifest_path)

    def test_rejects_duplicate_json_keys_at_every_depth(self) -> None:
        serialized = json.dumps(self.payload)
        documents = (
            serialized.replace(
                '"suite": "trixie"',
                '"suite": "trixie", "suite": "bookworm"',
                1,
            ),
            serialized.replace(
                f'"label": "{ROOT_LABEL}"',
                f'"label": "{ROOT_LABEL}", "label": "shadow"',
                1,
            ),
        )

        for document in documents:
            with self.subTest(document=document):
                self.manifest_path.write_text(document, encoding="utf-8")
                with self.assertRaisesRegex(ContractError, "duplicate JSON key"):
                    load_manifest(self.manifest_path)

    def test_wraps_malformed_json_as_contract_error(self) -> None:
        self.manifest_path.write_text('{"schema_version":', encoding="utf-8")

        with self.assertRaisesRegex(ContractError, "invalid manifest JSON"):
            load_manifest(self.manifest_path)

    def test_wraps_invalid_manifest_utf8_as_contract_error(self) -> None:
        self.manifest_path.write_bytes(b'{"suite":"trixie","bad":"\xff"}')

        with self.assertRaisesRegex(ContractError, "manifest must be UTF-8"):
            load_manifest(self.manifest_path)

    def test_rejects_booleans_where_integers_are_required(self) -> None:
        cases = (
            (("schema_version",), True),
            (("filesystem", "size_bytes"), True),
            (("filesystem", "block_size_bytes"), False),
        )

        for path, value in cases:
            with self.subTest(path=".".join(path)):
                payload = copy.deepcopy(self.payload)
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.write_manifest(payload)
                with self.assertRaisesRegex(ValueError, "must be an integer"):
                    load_manifest(self.manifest_path)

        self.write_manifest()
        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "must be an integer"):
            validate_frozen_root(
                self.image,
                replace(manifest, schema_version=True),
                self.packages_lock,
            )

    def test_rejects_non_https_provenance_urls(self) -> None:
        cases = (
            (("mirror_url",), "http://deb.debian.org/debian"),
            (
                ("signed_metadata", "url"),
                "file:///var/cache/debian/dists/trixie/InRelease",
            ),
        )

        for path, value in cases:
            with self.subTest(path=".".join(path)):
                payload = copy.deepcopy(self.payload)
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.write_manifest(payload)
                manifest = load_manifest(self.manifest_path)
                with self.assertRaisesRegex(ValueError, "HTTPS URL"):
                    validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_wraps_malformed_provenance_urls_as_contract_error(self) -> None:
        cases = (
            (("mirror_url",), "mirror_url"),
            (("signed_metadata", "url"), "signed_metadata.url"),
        )

        for path, expected_field in cases:
            with self.subTest(path=".".join(path)):
                payload = copy.deepcopy(self.payload)
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = "https://[invalid-authority"
                self.write_manifest(payload)
                manifest = load_manifest(self.manifest_path)
                with self.assertRaisesRegex(
                    ContractError,
                    rf"{expected_field}.*HTTPS URL",
                ):
                    validate_frozen_root(
                        self.image,
                        manifest,
                        self.packages_lock,
                    )

    def test_filesystem_errors_are_not_wrapped_as_contract_errors(self) -> None:
        missing_manifest = self.directory / "missing-manifest.json"
        with self.assertRaises(FileNotFoundError):
            load_manifest(missing_manifest)

        self.write_manifest()
        manifest = load_manifest(self.manifest_path)
        missing_image = self.directory / "missing-root.ext2"
        with self.assertRaises(FileNotFoundError):
            validate_frozen_root(
                missing_image,
                manifest,
                self.packages_lock,
            )

    def test_rejects_wrong_debian_and_filesystem_identity(self) -> None:
        cases = (
            (("suite",), "bookworm"),
            (("architecture",), "amd64"),
            (("filesystem", "type"), "ext4"),
            (("filesystem", "label"), "DEBIAN_ROOT"),
            (("filesystem", "size_bytes"), ROOT_IMAGE_SIZE_BYTES // 2),
            (("filesystem", "block_size_bytes"), 1024),
        )

        for path, value in cases:
            with self.subTest(path=".".join(path)):
                payload = copy.deepcopy(self.payload)
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.write_manifest(payload)
                manifest = load_manifest(self.manifest_path)
                with self.assertRaisesRegex(ValueError, "unexpected"):
                    validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_noncanonical_debian_release_versions(self) -> None:
        for release in (
            "",
            "12",
            "13",
            "13.",
            "13..6",
            "13.06",
            "13.6.1",
            "13.6a",
            " 13.6",
            "13.6 ",
        ):
            with self.subTest(release=release):
                self.payload["debian_release"] = release
                self.write_manifest()
                with self.assertRaises(ValueError):
                    manifest = load_manifest(self.manifest_path)
                    validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_malformed_sha256_values(self) -> None:
        paths = (
            ("signed_metadata", "sha256"),
            ("packages_lock_sha256",),
            ("downloaded_packages", 0, "sha256"),
            ("root_image_sha256",),
        )

        for path in paths:
            with self.subTest(path=".".join(str(part) for part in path)):
                payload = copy.deepcopy(self.payload)
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = "A" * 64
                self.write_manifest(payload)
                with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
                    load_manifest(self.manifest_path)

    def test_rejects_duplicate_and_unsorted_package_entries(self) -> None:
        cases = (
            PACKAGE_ROWS + (PACKAGE_ROWS[-1],),
            tuple(reversed(PACKAGE_ROWS)),
        )

        for rows in cases:
            with self.subTest(rows=rows):
                lock_text = _lock_text(rows)
                self.packages_lock.write_text(lock_text, encoding="utf-8")
                self.payload["packages_lock_sha256"] = _sha256_text(lock_text)
                self.write_manifest()
                manifest = load_manifest(self.manifest_path)
                with self.assertRaisesRegex(ValueError, "sorted and unique"):
                    validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_two_versions_for_one_package_architecture(self) -> None:
        rows = list(PACKAGE_ROWS)
        procps_index = rows.index(("procps", "riscv64", "2:4.0.4-9"))
        rows.insert(procps_index, ("procps", "riscv64", "2:4.0.4-8"))
        lock_text = _lock_text(tuple(rows))
        self.packages_lock.write_text(lock_text, encoding="utf-8")
        self.payload["packages_lock_sha256"] = _sha256_text(lock_text)
        self.write_manifest()

        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "package identities must be unique"):
            validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_unsorted_and_duplicate_downloaded_packages(self) -> None:
        downloaded_packages = self.payload["downloaded_packages"]
        cases = (
            (
                "sorted",
                list(reversed(downloaded_packages)),
            ),
            (
                "unique",
                downloaded_packages[:1] + downloaded_packages,
            ),
        )

        for expected_error, identities in cases:
            with self.subTest(expected_error=expected_error):
                self.payload["downloaded_packages"] = identities
                self.write_manifest()
                manifest = load_manifest(self.manifest_path)
                with self.assertRaisesRegex(ValueError, expected_error):
                    validate_frozen_root(
                        self.image,
                        manifest,
                        self.packages_lock,
                    )

    def test_rejects_downloaded_package_absent_from_lock(self) -> None:
        self.payload["downloaded_packages"][0]["version"] = "0.not-locked"
        self.write_manifest()

        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "does not match packages.lock"):
            validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_missing_explicit_install_download(self) -> None:
        self.payload["downloaded_packages"] = [
            identity
            for identity in self.payload["downloaded_packages"]
            if identity["name"] != "procps"
        ]
        self.write_manifest()

        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "missing explicit install packages"):
            validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_missing_non_explicit_locked_package_download(self) -> None:
        self.payload["downloaded_packages"] = [
            {
                "name": name,
                "architecture": architecture,
                "version": version,
                "sha256": hashlib.sha256(name.encode()).hexdigest(),
            }
            for name, architecture, version in PACKAGE_ROWS
            if name != "base-files"
        ]
        self.write_manifest()

        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "packages.lock set"):
            validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_manifest_package_lock_version_mismatch(self) -> None:
        self.payload["gate_packages"]["bash"] = "0.invalid"
        self.write_manifest()

        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "gate package bash version"):
            validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_base_image_size_and_hash_mismatch(self) -> None:
        self.write_manifest()
        manifest = load_manifest(self.manifest_path)
        short_image = self.directory / "short.ext2"
        short_image.write_bytes(b"not one GiB")

        with self.assertRaisesRegex(ValueError, "image size"):
            validate_frozen_root(short_image, manifest, self.packages_lock)

        payload = copy.deepcopy(self.payload)
        payload["root_image_sha256"] = "0" * 64
        self.write_manifest(payload)
        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "image SHA-256"):
            validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_rejects_package_lock_hash_mismatch(self) -> None:
        self.payload["packages_lock_sha256"] = "0" * 64
        self.write_manifest()

        manifest = load_manifest(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "package-lock SHA-256"):
            validate_frozen_root(self.image, manifest, self.packages_lock)

    def test_package_lock_validation_uses_one_open_file(self) -> None:
        original_lock = self.directory / "swap-packages.lock"
        replacement_lock = self.directory / "replacement-packages.lock"
        original_lock.write_text(_lock_text(), encoding="utf-8")
        replacement_text = "substituted\triscv64\t0.invalid\n"
        replacement_lock.write_text(replacement_text, encoding="utf-8")
        self.write_manifest()
        manifest = load_manifest(self.manifest_path)
        real_open = Path.open
        callback_count = 0

        def replace_after_open(path: Path, *args, **kwargs):
            nonlocal callback_count
            opened_file = real_open(path, *args, **kwargs)
            if path == original_lock:
                callback_count += 1
                if callback_count == 1:
                    replacement_lock.replace(path)
            return opened_file

        with mock.patch.object(Path, "open", new=replace_after_open):
            validated = validate_frozen_root(self.image, manifest, original_lock)

        self.assertEqual(callback_count, 1)
        self.assertEqual(validated.packages_lock_sha256, _sha256_text(_lock_text()))
        self.assertEqual(original_lock.read_text(encoding="utf-8"), replacement_text)

    def test_image_validation_uses_one_open_file(self) -> None:
        image = self.directory / "swap-root.ext2"
        replacement_image = self.directory / "replacement-root.ext2"
        with image.open("wb") as image_file:
            image_file.truncate(ROOT_IMAGE_SIZE_BYTES)
        replacement_bytes = b"short replacement image"
        replacement_image.write_bytes(replacement_bytes)
        self.write_manifest()
        manifest = load_manifest(self.manifest_path)
        real_open = Path.open
        callback_count = 0

        def replace_after_open(path: Path, *args, **kwargs):
            nonlocal callback_count
            opened_file = real_open(path, *args, **kwargs)
            if path == image:
                callback_count += 1
                if callback_count == 1:
                    replacement_image.replace(path)
            return opened_file

        with mock.patch.object(Path, "open", new=replace_after_open):
            validate_frozen_root(image, manifest, self.packages_lock)

        self.assertEqual(callback_count, 1)
        self.assertEqual(image.read_bytes(), replacement_bytes)


class DebianRootfsGateProtocolTests(unittest.TestCase):
    NONCE = "0123456789abcdef" * 4
    EXPECTED_PACKAGES = tuple(
        (name, version)
        for name, architecture, version in PACKAGE_ROWS
        if name in GATE_IDENTITY_PACKAGES and architecture == "riscv64"
    )

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.uboot = self._regular_file("u-boot", b"u-boot")
        self.boot_disk = self._regular_file("boot.ext4", b"ext4")
        self.root_disk = self._regular_file("debian-root.ext2", b"ext2")
        self.monitor_socket = self.directory / "monitor.sock"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _regular_file(self, name: str, contents: bytes) -> Path:
        path = self.directory / name
        path.write_bytes(contents)
        return path

    def _qemu_argv(self, **overrides: object) -> tuple[str, ...]:
        arguments: dict[str, object] = {
            "uboot": self.uboot,
            "boot_disk": self.boot_disk,
            "root_disk": self.root_disk,
            "monitor_socket": self.monitor_socket,
            "smp": 4,
            "dtb_enabled_cpu_count": 4,
        }
        arguments.update(overrides)
        return qemu_argv(**arguments)

    @staticmethod
    def _command_output(command: ShellCommand, *, boot_number: int) -> str:
        outputs = {
            "architecture": "riscv64",
            "debian-release": "13.6",
            "bash-version": "5.2.37(1)-release",
            "packages": "\n".join(
                f"{name}\t{version}"
                for name, version in DebianRootfsGateProtocolTests.EXPECTED_PACKAGES
            ),
            "root-filesystem": "ext2/ext3",
            "persistence": DebianRootfsGateProtocolTests.NONCE,
            "second-probe": "boot2-probe-created",
        }
        if command.name == "second-probe" and boot_number != 2:
            raise AssertionError("boot one must not create the second probe")
        return outputs[command.name]

    def _successful_transcript(
        self,
        commands: tuple[ShellCommand, ...],
        *,
        boot_number: int,
        echoed: bool = False,
    ) -> str:
        lines = ["Asterinas boot noise"]
        for command in commands:
            if echoed:
                lines.append(f"root@asterinas:/# {command.payload}")
            lines.extend(
                (
                    command.begin_marker,
                    self._command_output(command, boot_number=boot_number),
                    f"{command.status_prefix}0",
                    command.end_marker,
                )
            )
        return "\r\n".join(lines) + "\r\n"

    def _classify(
        self,
        transcript: str | bytes,
        commands: tuple[ShellCommand, ...],
        *,
        boot_number: int,
    ) -> GateResult:
        return classify_boot(
            transcript,
            commands,
            boot_number=boot_number,
            expected_debian_release="13.6",
            expected_packages=self.EXPECTED_PACKAGES,
            expected_nonce=self.NONCE,
        )

    def test_qemu_argv_is_the_frozen_headless_two_disk_contract(self) -> None:
        argv = self._qemu_argv()

        self.assertEqual(argv[0], "qemu-system-riscv64")
        self.assertEqual(argv[argv.index("-machine") + 1], "virt")
        self.assertEqual(
            argv[argv.index("-cpu") + 1],
            "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        )
        self.assertEqual(argv[argv.index("-m") + 1], "2G")
        self.assertEqual(argv[argv.index("-smp") + 1], "4")
        self.assertEqual(argv[argv.index("-serial") + 1], "stdio")
        self.assertEqual(argv[argv.index("-display") + 1], "none")
        self.assertEqual(argv[argv.index("-nic") + 1], "none")
        self.assertIn("-no-reboot", argv)

        drives = [
            argv[index + 1] for index, value in enumerate(argv) if value == "-drive"
        ]
        devices = [
            argv[index + 1] for index, value in enumerate(argv) if value == "-device"
        ]
        self.assertEqual(
            drives,
            [
                f"if=none,format=raw,file={self.boot_disk},id=bootdisk,readonly=on",
                (
                    f"if=none,format=raw,file={self.root_disk},id=rootdisk,"
                    "cache=directsync"
                ),
            ],
        )
        self.assertEqual(
            devices,
            [
                "virtio-blk-device,drive=bootdisk",
                "virtio-blk-device,drive=rootdisk",
            ],
        )
        forbidden = ("xhci", "usb", "keyboard", "tablet", "mouse", "netdev")
        self.assertFalse(
            any(token in argument.lower() for argument in argv for token in forbidden)
        )

    def test_qemu_argv_rejects_non_four_hart_contracts(self) -> None:
        for override in ({"smp": 1}, {"smp": True}, {"dtb_enabled_cpu_count": 3}):
            with (
                self.subTest(override=override),
                self.assertRaisesRegex(ValueError, "exactly 4"),
            ):
                self._qemu_argv(**override)

    def test_qemu_argv_rejects_unsafe_or_non_regular_inputs(self) -> None:
        comma_path = self._regular_file("boot,unsafe.ext4", b"ext4")
        missing_path = self.directory / "missing.ext2"
        symlink_path = self.directory / "root-link.ext2"
        symlink_path.symlink_to(self.root_disk)
        cases = (
            ("boot_disk", comma_path, "comma"),
            ("root_disk", missing_path, "regular file"),
            ("root_disk", symlink_path, "symbolic link"),
            ("uboot", self.directory, "regular file"),
        )

        for argument, path, message in cases:
            with (
                self.subTest(argument=argument, path=path),
                self.assertRaisesRegex(ValueError, message),
            ):
                self._qemu_argv(**{argument: path})

    def test_protocol_types_are_frozen(self) -> None:
        command = ShellCommand("name", "true", "begin", "end", "status=")
        evidence = BootEvidence(
            boot_number=1,
            architecture="riscv64",
            debian_release="13.6",
            bash_version="5.2",
            packages=self.EXPECTED_PACKAGES,
            root_filesystem="ext2/ext3",
            persistence_nonce=self.NONCE,
            second_probe=None,
        )
        result = GateResult(True, "pass", evidence)

        for instance in (command, evidence, result):
            with (
                self.subTest(instance=instance),
                self.assertRaises(FrozenInstanceError),
            ):
                instance.__setattr__(next(iter(instance.__dataclass_fields__)), None)

    def test_shell_commands_cover_boot_one_identity_write_and_sync(self) -> None:
        commands = shell_commands(boot_number=1, nonce=self.NONCE)

        self.assertEqual(
            tuple(command.name for command in commands),
            (
                "architecture",
                "debian-release",
                "bash-version",
                "packages",
                "root-filesystem",
                "persistence",
            ),
        )
        payload = "\n".join(command.payload for command in commands)
        for required in (
            "uname -m",
            "/etc/debian_version",
            "BASH_VERSION",
            "base-files libc6 bash coreutils util-linux",
            "stat -f",
            "/var/lib/asterinas-debian-m1",
            self.NONCE,
            "sync",
        ):
            self.assertIn(required, payload)
        self.assertIn("> /var/lib/asterinas-debian-m1/persist", payload)

    def test_shell_commands_cover_boot_two_read_probe_and_sync(self) -> None:
        commands = shell_commands(boot_number=2, nonce=self.NONCE)
        payloads = {command.name: command.payload for command in commands}

        self.assertIn(
            "cat /var/lib/asterinas-debian-m1/persist", payloads["persistence"]
        )
        self.assertNotIn(
            "> /var/lib/asterinas-debian-m1/persist", payloads["persistence"]
        )
        self.assertIn("second-probe", payloads["second-probe"])
        self.assertIn("sync", payloads["second-probe"])
        self.assertEqual(
            len({command.begin_marker for command in commands}), len(commands)
        )
        self.assertTrue(
            all(
                len(command.payload.encode()) <= MAX_COMMAND_PAYLOAD_BYTES
                for command in commands
            )
        )

    def test_shell_commands_reject_bad_boot_or_nonce(self) -> None:
        for arguments in (
            {"boot_number": 3, "nonce": self.NONCE},
            {"boot_number": 1, "nonce": "not-shell-safe"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                shell_commands(**arguments)

    def test_classify_boot_accepts_echoed_commands_and_exact_identity(self) -> None:
        for boot_number in (1, 2):
            with self.subTest(boot_number=boot_number):
                commands = shell_commands(boot_number=boot_number, nonce=self.NONCE)
                transcript = self._successful_transcript(
                    commands, boot_number=boot_number, echoed=True
                )

                result = self._classify(transcript, commands, boot_number=boot_number)

                self.assertTrue(result.passed)
                self.assertEqual(result.reason, "pass")
                self.assertEqual(result.evidence.architecture, "riscv64")
                self.assertEqual(result.evidence.packages, self.EXPECTED_PACKAGES)
                self.assertEqual(result.evidence.persistence_nonce, self.NONCE)
                self.assertEqual(
                    result.evidence.second_probe,
                    "boot2-probe-created" if boot_number == 2 else None,
                )

    def test_classify_boot_rejects_stale_duplicate_and_reordered_markers(self) -> None:
        commands = shell_commands(boot_number=1, nonce=self.NONCE)
        good = self._successful_transcript(commands, boot_number=1)
        blocks = good.splitlines()[1:]
        first_block = blocks[:4]
        second_block = blocks[4:8]
        cases = (
            (commands[0].begin_marker + "\n" + good, "duplicate"),
            ("\n".join(second_block + first_block + blocks[8:]), "reordered"),
        )

        for transcript, reason in cases:
            with self.subTest(reason=reason):
                result = self._classify(transcript, commands, boot_number=1)
                self.assertFalse(result.passed)
                self.assertIn(reason, result.reason)

    def test_classify_boot_rejects_nonzero_and_wrong_identity(self) -> None:
        commands = shell_commands(boot_number=1, nonce=self.NONCE)
        good = self._successful_transcript(commands, boot_number=1)
        cases = (
            (
                good.replace(
                    f"{commands[0].status_prefix}0", f"{commands[0].status_prefix}7"
                ),
                "status 7",
            ),
            (good.replace("13.6", "12.0", 1), "Debian release"),
            (good.replace("5.2.37-2+b5", "0.invalid", 1), "package versions"),
        )

        for transcript, reason in cases:
            with self.subTest(reason=reason):
                result = self._classify(transcript, commands, boot_number=1)
                self.assertFalse(result.passed)
                self.assertIn(reason, result.reason)

    def test_classify_boot_scans_complete_transcript_for_fatal_markers(self) -> None:
        commands = shell_commands(boot_number=1, nonce=self.NONCE)
        good = self._successful_transcript(commands, boot_number=1)
        fatal_markers = (
            "Kernel panic - not syncing",
            "reboot: Restarting system",
            "EXT2-fs error (device vdb)",
            "Buffer I/O error on dev vdb",
            "blk_update_request: I/O error",
            "DEBIAN_ROOTFS_FAIL reason=root_mount",
        )

        for marker in fatal_markers:
            with self.subTest(marker=marker):
                result = self._classify(good + marker + "\n", commands, boot_number=1)
                self.assertFalse(result.passed)
                self.assertIn("fatal transcript marker", result.reason)

    def test_classify_boot_rejects_oversized_transcript_and_payload(self) -> None:
        commands = shell_commands(boot_number=1, nonce=self.NONCE)
        oversized_command = replace(
            commands[0], payload="x" * (MAX_COMMAND_PAYLOAD_BYTES + 1)
        )
        cases = (
            (b"x" * (MAX_TRANSCRIPT_BYTES + 1), commands, "transcript exceeds"),
            (b"", (oversized_command, *commands[1:]), "payload exceeds"),
        )

        for transcript, candidate_commands, reason in cases:
            with self.subTest(reason=reason):
                result = self._classify(transcript, candidate_commands, boot_number=1)
                self.assertFalse(result.passed)
                self.assertIn(reason, result.reason)

    def test_classify_boot_rejects_missing_or_mismatched_persistence(self) -> None:
        commands = shell_commands(boot_number=2, nonce=self.NONCE)
        good = self._successful_transcript(commands, boot_number=2)
        cases = (
            (good.replace(self.NONCE, "", 1), "persistence nonce"),
            (good.replace(self.NONCE, "f" * 64, 1), "persistence nonce"),
            (good.replace("boot2-probe-created", "", 1), "second probe"),
        )

        for transcript, reason in cases:
            with self.subTest(reason=reason):
                result = self._classify(transcript, commands, boot_number=2)
                self.assertFalse(result.passed)
                self.assertIn(reason, result.reason)


class DebianRootfsGateRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    @staticmethod
    def _deadline(seconds: float = 1.0) -> float:
        return time.monotonic() + seconds

    def _hmp_server(
        self,
        chunks: tuple[bytes, ...],
        *,
        receive_command: bool = False,
        keep_open: float = 0.0,
    ) -> tuple[Path, threading.Thread, list[bytes]]:
        path = self.directory / f"hmp-{time.monotonic_ns()}.sock"
        ready = threading.Event()
        received: list[bytes] = []

        def serve() -> None:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                listener.listen(1)
                ready.set()
                connection, _ = listener.accept()
                with connection:
                    if receive_command:
                        connection.sendall(b"QEMU 10.0\r\n(qemu) ")
                        received.append(connection.recv(1024))
                    for chunk in chunks:
                        connection.sendall(chunk)
                        time.sleep(0.005)
                    if keep_open:
                        time.sleep(keep_open)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(1.0))
        return path, thread, received

    def test_serial_console_matches_split_markers_with_one_total_deadline(self) -> None:
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        console = SerialConsole(master, max_bytes=128)

        def write_split_marker() -> None:
            for chunk in (b"boot noi", b"se\r\nROOT_", b"READY\r\n"):
                os.write(slave, chunk)
                time.sleep(0.01)

        writer = threading.Thread(target=write_split_marker)
        writer.start()
        matched = console.wait_for(b"ROOT_READY", self._deadline())
        writer.join()
        console.send(b"echo ready\n", self._deadline())

        self.assertIn(b"ROOT_READY", matched)
        self.assertIn(b"boot noise", console.transcript)
        self.assertEqual(os.read(slave, 128), b"echo ready\n")

    def test_serial_console_bounds_prompt_boot_and_drain_deadlines(self) -> None:
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        console = SerialConsole(master, max_bytes=128)

        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "serial marker"):
            console.wait_for(b"never", started + 0.04)
        self.assertLess(time.monotonic() - started, 0.25)

        started = time.monotonic()
        drained = console.drain(started + 0.04)
        self.assertEqual(drained, b"")
        self.assertLess(time.monotonic() - started, 0.25)
        os.close(slave)

        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        console = SerialConsole(write_fd, max_bytes=128)
        with self.assertRaisesRegex(TimeoutError, "serial command"):
            console.send(b"x" * (1024 * 1024), self._deadline(0.04))
        os.close(write_fd)

    def test_serial_console_caps_transcript_and_reports_early_process_exit(
        self,
    ) -> None:
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        process = launch_process(("/bin/sh", "-c", "exit 23"))
        console = SerialConsole(read_fd, process=process, max_bytes=8)
        os.write(write_fd, b"123456789")
        os.close(write_fd)
        with self.assertRaisesRegex(BufferError, "serial transcript"):
            console.wait_for(b"missing", self._deadline())
        process.wait(self._deadline())

        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        os.close(write_fd)
        console = SerialConsole(read_fd, process=process, max_bytes=128)
        with self.assertRaises(EarlyProcessExit) as caught:
            console.wait_for(b"missing", self._deadline())
        self.assertEqual(caught.exception.returncode, 23)

    def test_hmp_connect_and_prompt_deadlines_are_bounded(self) -> None:
        missing = self.directory / "missing.sock"
        started = time.monotonic()
        with self.assertRaises(MonitorError):
            HmpMonitor.connect(missing, started + 0.04, max_response_bytes=64)
        self.assertLess(time.monotonic() - started, 0.25)

        path, thread, _ = self._hmp_server((b"not-a-prompt",), keep_open=0.15)
        started = time.monotonic()
        with self.assertRaisesRegex(MonitorError, "prompt"):
            HmpMonitor.connect(path, started + 0.04, max_response_bytes=64)
        self.assertLess(time.monotonic() - started, 0.25)
        thread.join(1.0)

    def test_hmp_matches_split_prompt_and_caps_each_response(self) -> None:
        path, thread, received = self._hmp_server(
            (b"info sta", b"tus\r\nrunning\r\n(qe", b"mu) "),
            receive_command=True,
        )
        monitor = HmpMonitor.connect(path, self._deadline(), max_response_bytes=128)
        self.addCleanup(monitor.close)
        response = monitor.command("info status", self._deadline())
        thread.join(1.0)
        self.assertIn(b"running", response)
        self.assertEqual(received, [b"info status\n"])

        path, thread, _ = self._hmp_server(
            (b"x" * 65,), receive_command=True, keep_open=0.05
        )
        monitor = HmpMonitor.connect(path, self._deadline(), max_response_bytes=64)
        with self.assertRaisesRegex(MonitorError, "exceeds"):
            monitor.command("query", self._deadline())
        monitor.close()
        thread.join(1.0)

    def test_pinned_output_rejects_symlink_and_survives_parent_swap(self) -> None:
        outside = self.directory / "outside"
        outside.mkdir()
        symlink = self.directory / "output-link"
        symlink.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            PinnedOutputDirectory(symlink)

        output = self.directory / "output"
        output.mkdir()
        moved = self.directory / "moved-output"
        with PinnedOutputDirectory(output) as pinned:
            output.rename(moved)
            output.symlink_to(outside, target_is_directory=True)
            pinned.atomic_write("result.json", b"safe")
        self.assertEqual((moved / "result.json").read_bytes(), b"safe")
        self.assertEqual(list(outside.iterdir()), [])

    def test_pinned_output_invalidates_stale_result_before_validation(self) -> None:
        output = self.directory / "output"
        output.mkdir()
        stale = output / "result.json"
        stale.write_bytes(b"stale pass")

        with PinnedOutputDirectory(output) as pinned:
            pinned.invalidate("result.json")
            with self.assertRaisesRegex(ValueError, "bad input"):
                raise ValueError("bad input")

        self.assertFalse(stale.exists())

    def test_pinned_output_atomic_write_copy_hash_and_directory_fsync(self) -> None:
        output = self.directory / "output"
        output.mkdir()
        source = self.directory / "source"
        source.write_bytes(b"copy contents")
        fsync_modes: list[int] = []
        real_fsync = gate_runtime_module.os.fsync

        def record_fsync(fd: int) -> None:
            fsync_modes.append(os.fstat(fd).st_mode)
            real_fsync(fd)

        with (
            mock.patch.object(gate_runtime_module.os, "fsync", new=record_fsync),
            PinnedOutputDirectory(output) as pinned,
        ):
            pinned.atomic_write("result.json", b"new result")
            pinned.atomic_copy("run-root.ext2", source)
            self.assertEqual(
                pinned.sha256("run-root.ext2"),
                hashlib.sha256(b"copy contents").hexdigest(),
            )

        self.assertEqual((output / "result.json").read_bytes(), b"new result")
        self.assertTrue(any(stat.S_ISDIR(mode) for mode in fsync_modes))
        self.assertTrue(any(stat.S_ISREG(mode) for mode in fsync_modes))
        self.assertFalse(
            any(path.name.startswith(".gate-") for path in output.iterdir())
        )

    def test_launch_starts_new_session_with_unblocked_termination_masks(self) -> None:
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        os.set_inheritable(slave, True)
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGHUP, signal.SIGTERM}
        )
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, previous_mask)
        script = (
            "import os,signal,sys; "
            "m=signal.pthread_sigmask(signal.SIG_BLOCK, []); "
            "inherited=1; "
            "\ntry: os.fstat(int(sys.argv[1]))\n"
            "except OSError: inherited=0\n"
            "print(os.getpid(),os.getsid(0),int(signal.SIGHUP in m),"
            "int(signal.SIGTERM in m),inherited,flush=True)"
        )
        process = launch_process(
            (sys.executable, "-c", script, str(slave)), stdio_fd=slave
        )
        os.close(slave)
        output = SerialConsole(master, process=process, max_bytes=256).drain(
            self._deadline()
        )
        self.assertEqual(process.wait(self._deadline()), 0)
        pid, sid, hup_blocked, term_blocked, inherited = output.decode().split()
        self.assertEqual(pid, sid)
        self.assertEqual((hup_blocked, term_blocked), ("0", "0"))
        self.assertEqual(inherited, "0")

    def test_cleanup_kills_group_after_leader_exit(self) -> None:
        child_pid_file = self.directory / "child.pid"
        script = (
            "import ctypes,os,time\n"
            "p=os.fork()\n"
            "if p:\n"
            f" open({str(child_pid_file)!r},'w').write(str(p))\n"
            " os._exit(0)\n"
            "ctypes.CDLL(None).prctl(15,b'gate child name',0,0,0)\n"
            "time.sleep(30)\n"
        )
        process = launch_process((sys.executable, "-c", script))
        self.assertEqual(process.wait(self._deadline()), 0)
        child_pid = int(child_pid_file.read_text())

        process.terminate_group(self._deadline(0.2), self._deadline(0.5))

        child_stat = Path(f"/proc/{child_pid}/stat")
        if child_stat.exists():
            _, _, remainder = child_stat.read_text(encoding="ascii").rpartition(") ")
            self.assertEqual(remainder.split()[0], "Z")

    def test_cleanup_escalates_term_to_kill_for_stubborn_group(self) -> None:
        script = (
            "import signal,time; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "print('ready',flush=True); time.sleep(30)"
        )
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        process = launch_process((sys.executable, "-c", script), stdio_fd=slave)
        os.close(slave)
        SerialConsole(master, process=process, max_bytes=128).wait_for(
            b"ready", self._deadline()
        )

        process.terminate_group(self._deadline(0.04), self._deadline(0.5))

        self.assertEqual(process.returncode, -signal.SIGKILL)

    def test_teardown_orders_monitor_group_cleanup_and_serial_drain(self) -> None:
        events: list[str] = []

        class RecordingMonitor:
            def close(self) -> None:
                events.append("monitor")

        class RecordingProcess:
            def terminate_group(
                self, term_deadline: float, kill_deadline: float
            ) -> None:
                events.append("process")

        class RecordingSerial:
            def drain(self, deadline: float) -> bytes:
                events.append("serial")
                return b"tail"

        tail = teardown_gate(
            RecordingMonitor(),
            RecordingProcess(),
            RecordingSerial(),
            term_deadline=self._deadline(),
            kill_deadline=self._deadline(),
            drain_deadline=self._deadline(),
        )
        self.assertEqual(events, ["monitor", "process", "serial"])
        self.assertEqual(tail, b"tail")

        events.clear()

        class FailingMonitor:
            def close(self) -> None:
                events.append("monitor")
                raise MonitorError("close failed")

        with self.assertRaisesRegex(MonitorError, "close failed"):
            teardown_gate(
                FailingMonitor(),
                RecordingProcess(),
                RecordingSerial(),
                term_deadline=self._deadline(),
                kill_deadline=self._deadline(),
                drain_deadline=self._deadline(),
            )
        self.assertEqual(events, ["monitor", "process", "serial"])

    def test_first_hup_or_term_is_deferred_through_publication_and_restored(
        self,
    ) -> None:
        for signum in (signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signum=signum):
                previous = signal.getsignal(signum)
                result = self.directory / f"result-{signum}.json"
                with self.assertRaises(GateTermination) as caught:
                    with TerminationSignalState():
                        os.kill(os.getpid(), signum)
                        result.write_text("published", encoding="utf-8")
                self.assertEqual(caught.exception.signum, signum)
                self.assertEqual(result.read_text(encoding="utf-8"), "published")
                self.assertIs(signal.getsignal(signum), previous)

    def test_second_scoped_termination_signal_hard_exits(self) -> None:
        pid = os.fork()
        if pid == 0:
            with TerminationSignalState():
                os.kill(os.getpid(), signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGTERM)
            os._exit(99)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 128 + signal.SIGTERM)


class DebianRootfsGateOrchestrationTests(unittest.TestCase):
    NONCE = "0123456789abcdef" * 4

    class Operations:
        def __init__(self, failure: str | None = None) -> None:
            self.failure = failure
            self.events: list[str] = []
            self.publications: list[dict[str, object]] = []

        def _event(self, name: str) -> None:
            self.events.append(name)
            if self.failure == name:
                raise GateFailure(name)

        def invalidate(self, config: GateConfig) -> None:
            del config
            self._event("invalidate")

        def snapshot_inputs(self, config: GateConfig) -> dict[str, str]:
            del config
            self._event("snapshot")
            return {"kernel": "a" * 64, "root_image": "b" * 64}

        def validate_inputs(
            self, config: GateConfig, snapshots: dict[str, str]
        ) -> dict[str, object]:
            del config, snapshots
            self._event("validate")
            return {
                "debian_release": "13.6",
                "packages": (("bash", "5.2"),),
            }

        def prepare(
            self,
            config: GateConfig,
            snapshots: dict[str, str],
            identity: dict[str, object],
        ) -> dict[str, str]:
            del config, snapshots, identity
            self._event("prepare")
            return {"boot_disk": "boot.ext4", "root_disk": "root.ext2"}

        def launch(
            self, config: GateConfig, prepared: dict[str, str], boot_number: int
        ) -> dict[str, object]:
            del config, prepared
            self._event(f"launch{boot_number}")
            return {
                "boot_number": boot_number,
                "argv": ("qemu-system-riscv64", f"boot={boot_number}"),
            }

        def drive_uboot(self, session: dict[str, object], config: GateConfig) -> None:
            del config
            self._event(f"uboot{session['boot_number']}")

        def enter_debian(self, session: dict[str, object], config: GateConfig) -> None:
            del config
            self._event(f"shell{session['boot_number']}")

        def execute_checks(
            self,
            session: dict[str, object],
            config: GateConfig,
            identity: dict[str, object],
            nonce: str,
        ) -> None:
            del config, identity, nonce
            self._event(f"commands{session['boot_number']}")

        def request_quit(self, session: dict[str, object], config: GateConfig) -> None:
            del config
            self._event(f"quit{session['boot_number']}")

        def close_monitor(self, session: dict[str, object]) -> None:
            self._event(f"close{session['boot_number']}")

        def cleanup_process(
            self, session: dict[str, object], config: GateConfig
        ) -> None:
            del config
            self._event(f"cleanup{session['boot_number']}")

        def drain_serial(self, session: dict[str, object], config: GateConfig) -> bytes:
            del config
            boot_number = session["boot_number"]
            self._event(f"drain{boot_number}")
            return f"complete boot {boot_number}\n".encode()

        def hash_final_root(self, config: GateConfig, prepared: dict[str, str]) -> str:
            del config, prepared
            self._event("hash-final-root")
            return "c" * 64

        def publish(
            self,
            config: GateConfig,
            prepared: dict[str, str] | None,
            transcripts: tuple[bytes, bytes],
            result: dict[str, object],
        ) -> None:
            del config, prepared, transcripts
            self._event("publish")
            self.publications.append(copy.deepcopy(result))

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        directory = Path(self.temporary_directory.name)
        inputs = []
        for name in (
            "kernel",
            "u-boot",
            "qemu-virt.dtb",
            "stage1-initramfs.cpio",
            "debian-root.ext2",
            "rootfs-manifest.json",
            "packages.lock",
            "package-checksums",
        ):
            path = directory / name
            path.write_bytes(name.encode())
            inputs.append(path)
        self.config = GateConfig(*inputs, directory / "output")

    def test_configuration_is_frozen_and_has_exact_four_hart_timeouts(self) -> None:
        self.assertEqual(self.config.smp, 4)
        self.assertGreater(self.config.boot_timeout, 0)
        self.assertGreater(self.config.command_timeout, 0)
        self.assertGreater(self.config.cleanup_timeout, 0)
        with self.assertRaises(FrozenInstanceError):
            self.config.smp = 1

    def test_success_uses_exact_two_boot_lifecycle_and_same_prepared_root(self) -> None:
        operations = self.Operations()

        result = orchestrate_gate(self.config, operations, nonce=self.NONCE)

        self.assertEqual(
            operations.events,
            [
                "invalidate",
                "snapshot",
                "validate",
                "prepare",
                "launch1",
                "uboot1",
                "shell1",
                "commands1",
                "quit1",
                "close1",
                "cleanup1",
                "drain1",
                "launch2",
                "uboot2",
                "shell2",
                "commands2",
                "quit2",
                "close2",
                "cleanup2",
                "drain2",
                "hash-final-root",
                "publish",
            ],
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["reason"], "pass")
        self.assertEqual(
            result["nonce_sha256"], hashlib.sha256(self.NONCE.encode()).hexdigest()
        )
        self.assertNotIn(self.NONCE, json.dumps(result))
        self.assertEqual(len(result["qemu_argv"]), 2)
        self.assertEqual(
            result["manifest_identity"],
            {"debian_release": "13.6"},
        )
        self.assertEqual(result["package_identity"], (("bash", "5.2"),))
        self.assertEqual(
            set(result["phase_durations_seconds"]),
            {"snapshot", "validate", "prepare", "boot1", "boot2", "hash-final-root"},
        )
        self.assertTrue(
            all(
                isinstance(duration, float) and duration >= 0
                for duration in result["phase_durations_seconds"].values()
            )
        )

    def test_failures_never_publish_a_passing_result_and_always_drain_launched_boot(
        self,
    ) -> None:
        cases = (
            ("prepare", []),
            ("launch1", []),
            ("uboot1", ["close1", "cleanup1", "drain1"]),
            ("shell1", ["close1", "cleanup1", "drain1"]),
            ("commands1", ["close1", "cleanup1", "drain1"]),
            ("drain1", ["close1", "cleanup1", "drain1"]),
            ("cleanup1", ["close1", "cleanup1", "drain1"]),
            ("hash-final-root", ["close2", "cleanup2", "drain2"]),
        )

        for failure, required_tail in cases:
            with self.subTest(failure=failure):
                operations = self.Operations(failure)
                result = orchestrate_gate(self.config, operations, nonce=self.NONCE)
                self.assertFalse(result["passed"])
                self.assertEqual(result["reason"], failure)
                for event in required_tail:
                    self.assertIn(event, operations.events)
                self.assertFalse(
                    any(
                        publication["passed"] for publication in operations.publications
                    )
                )

    def test_interrupted_publication_cannot_leave_passing_evidence(self) -> None:
        operations = self.Operations("publish")

        with self.assertRaisesRegex(GateFailure, "publish"):
            orchestrate_gate(self.config, operations, nonce=self.NONCE)

        self.assertEqual(operations.events[0], "invalidate")
        self.assertEqual(operations.events[-1], "publish")
        self.assertEqual(operations.publications, [])


class DebianRootfsGateArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def test_real_boot_image_is_64_mib_and_contains_exactly_three_files(self) -> None:
        kernel = self.directory / "kernel"
        dtb = self.directory / "dtb"
        initramfs = self.directory / "initramfs"
        kernel.write_bytes(b"kernel")
        dtb.write_bytes(b"dtb")
        initramfs.write_bytes(b"initramfs")
        image = self.directory / "boot.ext4"

        build_boot_image(kernel, dtb, initramfs, image)

        self.assertEqual(image.stat().st_size, 64 * 1024 * 1024)
        listing = subprocess.run(
            ["debugfs", "-R", "ls -p /", str(image)],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        names = {
            fields[5]
            for line in listing.splitlines()
            if len(fields := line.split("/")) >= 6 and fields[5] not in (".", "..", "")
        }
        self.assertEqual(
            names,
            {"asterinas.booti", "qemu-virt.dtb", "stage1-initramfs.cpio"},
        )
        self.assertNotIn("lost+found", listing)

    def test_sparse_root_copy_uses_descriptor_and_preserves_source_identity(
        self,
    ) -> None:
        source = self.directory / "base.ext2"
        with source.open("wb") as stream:
            stream.write(b"root")
            stream.seek(16 * 1024 * 1024 - 1)
            stream.write(b"\0")
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        self.addCleanup(os.close, descriptor)
        destination = self.directory / "run.ext2"

        before, after, copied = copy_sparse_root(descriptor, destination)

        self.assertEqual(before, after)
        self.assertEqual(copied, before)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertLess(destination.stat().st_blocks * 512, destination.stat().st_size)

    def test_fdtget_accepts_exactly_four_enabled_cpu_nodes(self) -> None:
        source = self.directory / "four.dts"
        dtb = self.directory / "four.dtb"
        cpus = "\n".join(
            f'cpu@{index} {{ device_type = "cpu"; reg = <{index}>; status = "okay"; }};'
            for index in range(4)
        )
        source.write_text(
            "/dts-v1/; / { #address-cells = <1>; #size-cells = <1>; "
            f"cpus {{ #address-cells = <1>; #size-cells = <0>; {cpus} }}; }};",
            encoding="ascii",
        )
        subprocess.run(["dtc", "-I", "dts", "-O", "dtb", "-o", dtb, source], check=True)

        self.assertEqual(verify_four_hart_dtb(dtb), 4)

        source.write_text(
            source.read_text().replace('status = "okay";', 'status = "disabled";', 1),
            encoding="ascii",
        )
        subprocess.run(["dtc", "-I", "dts", "-O", "dtb", "-o", dtb, source], check=True)
        with self.assertRaisesRegex(GateFailure, "exactly 4"):
            verify_four_hart_dtb(dtb)

    def test_cli_requires_all_eight_inputs_and_output_without_build_mode(self) -> None:
        required = (
            "--kernel",
            "--uboot",
            "--dtb",
            "--stage1-initramfs",
            "--root-image",
            "--root-manifest",
            "--packages-lock",
            "--package-checksums",
            "--output-directory",
        )
        arguments = [
            value for option in required for value in (option, f"/{option[2:]}")
        ]

        config = parse_gate_args(arguments)

        self.assertEqual(config.smp, 4)
        with self.assertRaises(SystemExit):
            parse_gate_args(arguments[:-2])
        for forbidden in ("download", "rebuild", "mirror"):
            self.assertNotIn(forbidden, " ".join(arguments))
        with mock.patch.object(
            gate_backend_module, "main", return_value=7
        ) as backend_main:
            self.assertEqual(rootfs_gate_module.main(arguments), 7)
        backend_main.assert_called_once_with(arguments)


class DebianRootfsGateBackendSessionTests(unittest.TestCase):
    def test_session_uses_hardlinked_same_root_and_removes_directory_after_drain(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            inputs = []
            for index in range(8):
                path = directory / f"input-{index}"
                path.write_bytes(str(index).encode())
                inputs.append(path)
            output = directory / "output"
            output.mkdir()
            boot = output / "boot.ext4"
            root = output / "debian-root.run.ext2"
            boot.write_bytes(b"boot")
            root.write_bytes(b"root")
            config = GateConfig(*inputs, output)
            process = mock.Mock()
            monitor = mock.Mock()
            serial = mock.Mock(transcript=b"")
            serial.drain.return_value = b""
            with (
                mock.patch.object(
                    gate_backend_module, "launch_process", return_value=process
                ),
                mock.patch.object(
                    gate_backend_module.HmpMonitor, "connect", return_value=monitor
                ),
                mock.patch.object(
                    gate_backend_module, "SerialConsole", return_value=serial
                ),
                gate_backend_module.ConcreteOperations(config) as operations,
            ):
                session = operations.launch(
                    config, {"boot_disk": boot, "root_disk": root}, 1
                )
                session_directory = session["directory"]
                self.assertTrue(os.path.samefile(root, session_directory / root.name))
                operations.close_monitor(session)
                operations.cleanup_process(session, config)
                self.assertEqual(operations.drain_serial(session, config), b"")
                self.assertFalse(session_directory.exists())

    def test_shell_wait_marker_is_not_present_in_echoed_command_payload(self) -> None:
        class EchoingSerial:
            def __init__(self) -> None:
                self.payload = b""

            def send(self, payload: bytes, deadline: float) -> None:
                del deadline
                self.payload = payload

            def wait_for(self, marker: bytes, deadline: float) -> bytes:
                del deadline
                if marker in self.payload:
                    raise AssertionError("echoed payload matched completion marker")
                return self.payload + marker + b"\r\n"

        operations = object.__new__(gate_backend_module.ConcreteOperations)
        session = {"boot_number": 1, "serial": EchoingSerial()}
        operations.execute_checks(
            session,
            mock.Mock(command_timeout=1.0),
            {"debian_release": "13.6", "packages": ()},
            "0123456789abcdef" * 4,
        )
        self.assertIn("commands", session)


if __name__ == "__main__":
    unittest.main()
