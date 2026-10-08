#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import gzip
import json
import os
import select
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from qemu_uboot_commands import qemu_argv  # noqa: E402
from qemu_uboot_devices import DRM_GEM, device_set_by_name  # noqa: E402
from qemu_uboot_profiles import (  # noqa: E402
    GENERIC_SV39_DRM_GEM_SMP4,
    profile_by_name,
)
from drm.gem_gate import (  # noqa: E402
    CLOSE_MARKER,
    CREATE_MARKER,
    FLINK_MARKER,
    MAX_TRANSCRIPT_BYTES,
    OPEN_MARKER,
    READY_MARKER,
    REJECT_MARKER,
    GemGateConfig,
    classify_transcript,
    run_gem_gate,
)


class DrmGemLaunchContractTests(unittest.TestCase):
    def test_registered_profile_is_generic_sv39_smp4(self) -> None:
        profile = profile_by_name("generic-sv39-drm-gem-smp4")
        self.assertIs(profile, GENERIC_SV39_DRM_GEM_SMP4)
        self.assertEqual(profile.hart_count, 4)
        self.assertEqual(profile.memory, "2G")
        self.assertEqual(profile.mmu_type, "riscv,sv39")
        self.assertEqual(profile.validation.completion_line, READY_MARKER)

    def test_qemu_argv_has_only_the_gem_device_contract(self) -> None:
        self.assertIs(device_set_by_name("drm-gem"), DRM_GEM)
        argv = qemu_argv(
            uboot=Path("/inputs/u-boot"),
            boot_disk=Path("/inputs/boot.ext4"),
            profile=GENERIC_SV39_DRM_GEM_SMP4,
            device_set=DRM_GEM,
        )
        joined = " ".join(argv)
        self.assertIn("-smp 4", joined)
        self.assertIn("-m 2G", joined)
        self.assertIn("rv64,sv48=false", joined)
        self.assertIn("-display none", joined)
        self.assertIn("-nic none", joined)
        self.assertIn("-device virtio-gpu-device", joined)
        self.assertNotIn("virtio-net", joined)
        self.assertNotIn("virtio-keyboard", joined)

    def test_gem_gate_names_its_own_device_set(self) -> None:
        # The gate must not borrow the cursor gate's set: a device change made
        # for one gate should never silently move the other.
        self.assertIsNot(DRM_GEM, device_set_by_name("drm-cursor"))
        self.assertEqual(DRM_GEM.name, "drm-gem")


class DrmGemClassifierTests(unittest.TestCase):
    @staticmethod
    def passing_transcript() -> bytes:
        return b"\n".join(
            (
                CREATE_MARKER,
                FLINK_MARKER,
                OPEN_MARKER,
                CLOSE_MARKER,
                REJECT_MARKER,
                READY_MARKER,
            )
        )

    def test_requires_the_ordered_guest_sequence(self) -> None:
        result = classify_transcript(self.passing_transcript())
        self.assertTrue(result.passed, result.reason)
        self.assertEqual(result.stage_count, 6)
        self.assertEqual(result.failed_stage, "")

    def test_rejects_a_missing_or_reordered_stage(self) -> None:
        full = self.passing_transcript()
        for marker in (CREATE_MARKER, FLINK_MARKER, OPEN_MARKER, CLOSE_MARKER, REJECT_MARKER):
            with self.subTest(marker=marker):
                without = full.replace(marker + b"\n", b"", 1)
                self.assertFalse(classify_transcript(without).passed)

        # Same markers, but open before flink: the object cannot exist yet.
        swapped = b"\n".join(
            (CREATE_MARKER, OPEN_MARKER, FLINK_MARKER, CLOSE_MARKER, REJECT_MARKER, READY_MARKER)
        )
        result = classify_transcript(swapped)
        self.assertFalse(result.passed)
        self.assertEqual(result.failed_stage, "")

    def test_surfaces_the_guests_own_diagnosis(self) -> None:
        transcript = b"\n".join(
            (
                CREATE_MARKER,
                b"DRM_GEM_FAIL stage=map-reopened errno=22",
            )
        )
        result = classify_transcript(transcript)
        self.assertFalse(result.passed)
        self.assertEqual(result.failed_stage, "map-reopened")
        self.assertIn("map-reopened", result.reason)
        self.assertIn("22", result.reason)

    def test_scans_the_full_bounded_transcript_for_fatal_output(self) -> None:
        for fatal in (b"Uncaught panic", b"unexpected exception"):
            with self.subTest(fatal=fatal):
                transcript = self.passing_transcript() + b"\n" + fatal
                self.assertFalse(classify_transcript(transcript).passed)

        with self.assertRaises(ValueError):
            classify_transcript(b"x" * (MAX_TRANSCRIPT_BYTES + 1))

    def test_runtime_invalidates_stale_success_and_publishes_final_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "evidence"
            output.mkdir()
            (output / "result.json").write_text('{"passed": true}\n')

            def fake_runner(**arguments: object) -> object:
                Path(arguments["serial_log"]).write_bytes(self.passing_transcript())
                return type("BaseResult", (), {"passed": True})()

            result = run_gem_gate(
                GemGateConfig(
                    uboot=Path("/inputs/u-boot"),
                    boot_disk=Path("/inputs/boot.ext4"),
                    manifest=Path("/inputs/artifacts.json"),
                    output_directory=output,
                ),
                runner=fake_runner,
            )
            self.assertTrue(result.passed, result.reason)
            self.assertEqual(
                json.loads((output / "result.json").read_text())["passed"], True
            )

    def test_runtime_failure_never_leaves_stale_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "evidence"
            output.mkdir()
            (output / "result.json").write_text('{"passed": true}\n')

            def failing_runner(**_arguments: object) -> object:
                raise RuntimeError("launch failed")

            result = run_gem_gate(
                GemGateConfig(
                    uboot=Path("/inputs/u-boot"),
                    boot_disk=Path("/inputs/boot.ext4"),
                    manifest=Path("/inputs/artifacts.json"),
                    output_directory=output,
                ),
                runner=failing_runner,
            )
            self.assertFalse(result.passed)
            self.assertEqual(
                json.loads((output / "result.json").read_text())["passed"], False
            )


class DrmGemGuestProbeTests(unittest.TestCase):
    SELF_TEST_CASE_NAMES = (
        "valid",
        "create-error",
        "map-error",
        "flink-error",
        "flink-repeat-error",
        "open-error",
        "map-reopened-error",
        "close-error",
        "close-lost-error",
        "release-error",
        "reject-accepted",
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = TOOLS / "drm/gem_gate_init.c"

    def compile_probe(
        self, output: Path, define: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "cc",
                "-std=c11",
                "-O2",
                "-static",
                "-Wall",
                "-Wextra",
                "-Werror",
                f"-D{define}",
                os.fspath(self.source),
                "-o",
                os.fspath(output),
            ],
            capture_output=True,
            check=False,
            text=True,
        )

    def test_native_self_test_covers_every_gem_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            binary = Path(temporary_directory) / "gem-self-test"
            result = self.compile_probe(binary, "DRM_GEM_GATE_SELF_TEST")
            self.assertEqual(result.returncode, 0, result.stderr)
            for case in self.SELF_TEST_CASE_NAMES:
                with self.subTest(case=case):
                    run = subprocess.run(
                        [binary, case], capture_output=True, check=False, text=True
                    )
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertEqual(
                        run.stdout, f"DRM_GEM_SELF_TEST PASS case={case}\n"
                    )

    def test_self_test_rejects_an_unknown_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            binary = Path(temporary_directory) / "gem-self-test"
            result = self.compile_probe(binary, "DRM_GEM_GATE_SELF_TEST")
            self.assertEqual(result.returncode, 0, result.stderr)
            run = subprocess.run(
                [binary, "not-a-case"], capture_output=True, check=False, text=True
            )
            self.assertEqual(run.returncode, 2)

    def test_success_lifecycle_publishes_markers_then_holds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            binary = Path(temporary_directory) / "gem-lifecycle"
            result = self.compile_probe(binary, "DRM_GEM_GATE_LIFECYCLE_TEST")
            self.assertEqual(result.returncode, 0, result.stderr)
            with subprocess.Popen(
                [binary], stdout=subprocess.PIPE, stderr=subprocess.PIPE
            ) as process:
                try:
                    assert process.stdout is not None
                    output = b""
                    deadline = time.monotonic() + 2.0
                    while output.count(b"\n") < 6:
                        readable, _, _ = select.select(
                            [process.stdout], [], [], max(0.0, deadline - time.monotonic())
                        )
                        self.assertTrue(readable, "GEM lifecycle marker timed out")
                        output += os.read(process.stdout.fileno(), 4096)
                    self.assertEqual(
                        output.decode().splitlines(),
                        [
                            CREATE_MARKER.decode(),
                            FLINK_MARKER.decode(),
                            OPEN_MARKER.decode(),
                            CLOSE_MARKER.decode(),
                            REJECT_MARKER.decode(),
                            READY_MARKER.decode(),
                        ],
                    )
                    self.assertIsNone(process.poll())
                finally:
                    process.terminate()
                    process.wait(timeout=2.0)


class DrmGemBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.builder = TOOLS / "drm/build_gem_gate.sh"

    @staticmethod
    def make_fake_compiler(directory: Path) -> tuple[Path, Path]:
        elf = directory / "init.riscv64"
        payload = bytearray(64)
        payload[:5] = b"\x7fELF\x02"
        struct.pack_into("<H", payload, 18, 243)
        elf.write_bytes(payload)
        compiler = directory / "fake-riscv-cc"
        compiler.write_text(
            "#!/bin/sh\n"
            'while [ "$1" != -o ]; do shift; done\n'
            "shift\n"
            'cp -- "$FAKE_RISCV_ELF" "$1"\n'
        )
        compiler.chmod(0o755)
        return compiler, elf

    def test_builder_is_deterministic_and_has_closed_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            compiler, elf = self.make_fake_compiler(directory)
            first = directory / "first gem.cpio.gz"
            second = directory / "second gem.cpio.gz"
            environment = os.environ | {
                "RISC_V_CC": os.fspath(compiler),
                "FAKE_RISCV_ELF": os.fspath(elf),
            }
            for output in (first, second):
                run = subprocess.run(
                    [self.builder, output],
                    env=environment,
                    capture_output=True,
                    check=False,
                    text=True,
                )
                self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o644)
            entries = parse_newc(gzip.decompress(first.read_bytes()))
            self.assertEqual(
                entries,
                [
                    ("dev", stat.S_IFDIR | 0o755, 0, 0, 0),
                    ("proc", stat.S_IFDIR | 0o755, 0, 0, 0),
                    ("sys", stat.S_IFDIR | 0o755, 0, 0, 0),
                    ("tmp", stat.S_IFDIR | 0o1777, 0, 0, 0),
                    ("init", stat.S_IFREG | 0o755, 0, 0, 0),
                ],
            )

    def test_builder_cli_and_failure_preserve_outputs(self) -> None:
        tools = subprocess.run(
            [self.builder, "--print-tools"],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(tools.returncode, 0, tools.stderr)
        self.assertEqual(tools.stdout, "riscv64-linux-gnu-gcc\npython3\n")

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "gem.cpio.gz"
            output.write_bytes(b"old archive")
            run = subprocess.run(
                [self.builder, output],
                env=os.environ | {"RISC_V_CC": "missing-riscv-compiler"},
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(output.read_bytes(), b"old archive")


def parse_newc(archive: bytes) -> list[tuple[str, int, int, int, int]]:
    entries: list[tuple[str, int, int, int, int]] = []
    offset = 0
    while True:
        header = archive[offset : offset + 110]
        if header[:6] != b"070701":
            raise ValueError("invalid newc header")
        fields = tuple(
            int(header[field_offset : field_offset + 8], 16)
            for field_offset in range(6, 110, 8)
        )
        mode, uid, gid = fields[1:4]
        mtime, file_size, name_size = fields[5], fields[6], fields[11]
        name_start = offset + 110
        name_end = name_start + name_size
        name = archive[name_start : name_end - 1].decode()
        data_start = (name_end + 3) & ~3
        offset = (data_start + file_size + 3) & ~3
        if name == "TRAILER!!!":
            return entries
        entries.append((name, mode, uid, gid, mtime))


if __name__ == "__main__":
    unittest.main()
