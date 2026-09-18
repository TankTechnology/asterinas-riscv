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
from qemu_uboot_devices import DRM_GEM, DRM_VIRGL, device_set_by_name  # noqa: E402
from qemu_uboot_profiles import (  # noqa: E402
    GENERIC_SV39_DRM_VIRGL_SMP4,
    profile_by_name,
)
from drm.virgl_param_gate import (  # noqa: E402
    CONTEXT_MARKER,
    MAX_TRANSCRIPT_BYTES,
    READY_MARKER,
    VirglParamGateConfig,
    classify_transcript,
    expects_3d,
    run_virgl_param_gate,
)


class DrmVirglLaunchContractTests(unittest.TestCase):
    def test_registered_profile_is_generic_sv39_smp4(self) -> None:
        profile = profile_by_name("generic-sv39-drm-virgl-smp4")
        self.assertIs(profile, GENERIC_SV39_DRM_VIRGL_SMP4)
        self.assertEqual(profile.hart_count, 4)
        self.assertEqual(profile.memory, "2G")
        self.assertEqual(profile.validation.completion_line, READY_MARKER)

    def test_qemu_argv_asks_for_the_gl_device_and_a_gl_backend(self) -> None:
        self.assertIs(device_set_by_name("drm-virgl"), DRM_VIRGL)
        argv = qemu_argv(
            uboot=Path("/inputs/u-boot"),
            boot_disk=Path("/inputs/boot.ext4"),
            profile=GENERIC_SV39_DRM_VIRGL_SMP4,
            device_set=DRM_VIRGL,
        )
        joined = " ".join(argv)
        self.assertIn("-device virtio-gpu-gl-device", joined)
        self.assertIn("-display egl-headless,gl=on", joined)
        self.assertIn("-nic none", joined)
        self.assertNotIn("-display none", joined)
        self.assertNotIn("virtio-gpu-device ", joined)

    def test_a_gl_device_without_a_gl_backend_is_refused(self) -> None:
        # The pairing is what produces the feature bit; a headless backend
        # would silently yield a device without 3D and a gate that proves
        # nothing.
        from qemu_uboot_devices import (
            DeviceKind,
            QemuDeviceSet,
            validate_registered_device_set,
        )

        with self.assertRaises(ValueError):
            validate_registered_device_set(
                QemuDeviceSet("bad", (DeviceKind.VIRTIO_GPU_GL,), display="none")
            )

    def test_the_control_device_set_is_the_same_probe_without_3d(self) -> None:
        self.assertTrue(expects_3d(DRM_VIRGL))
        self.assertFalse(expects_3d(DRM_GEM))


class DrmVirglClassifierTests(unittest.TestCase):
    @staticmethod
    def transcript(three_d: int, capsets: str, caps_bytes: int = 512) -> bytes:
        return b"\n".join(
            (
                f"DRM_VIRGL_PARAM 3d={three_d} capsets=0x{capsets}".encode(),
                f"DRM_VIRGL_CAPS PASS caps_bytes={caps_bytes}".encode(),
                CONTEXT_MARKER,
                READY_MARKER,
            )
        ) + b"\n"

    def test_accepts_a_host_that_reports_3d_and_its_capset(self) -> None:
        result = classify_transcript(self.transcript(1, "2"), expected_3d=True)
        self.assertTrue(result.passed, result.reason)
        self.assertEqual(result.reported_3d, 1)
        self.assertEqual(result.reported_capsets, 0x2)
        self.assertEqual(result.expected_3d, 1)
        self.assertEqual(result.caps_bytes, 512)

    def test_accepts_the_control_run_that_reports_no_3d(self) -> None:
        result = classify_transcript(
            self.transcript(0, "0", caps_bytes=0), expected_3d=False
        )
        self.assertTrue(result.passed, result.reason)
        self.assertEqual(result.expected_3d, 0)

    def test_rejects_a_report_that_contradicts_the_launched_device(self) -> None:
        # Both directions: a GL device that reports no 3D, and a plain device
        # that claims 3D. Either means the report is not tracking the device.
        no_3d = classify_transcript(
            self.transcript(0, "0", caps_bytes=0), expected_3d=True
        )
        self.assertFalse(no_3d.passed)
        self.assertIn("should report 1", no_3d.reason)

        claimed = classify_transcript(self.transcript(1, "2"), expected_3d=False)
        self.assertFalse(claimed.passed)
        self.assertIn("should report 0", claimed.reason)

    def test_rejects_a_3d_claim_without_a_capset(self) -> None:
        result = classify_transcript(self.transcript(1, "0"), expected_3d=True)
        self.assertFalse(result.passed)
        self.assertIn("lacks virgl", result.reason)

    def test_rejects_a_control_run_that_still_names_a_capset(self) -> None:
        result = classify_transcript(
            self.transcript(0, "2", caps_bytes=0), expected_3d=False
        )
        self.assertFalse(result.passed)
        self.assertIn("non-empty", result.reason)

    def test_rejects_a_blob_that_was_never_delivered(self) -> None:
        # Success without a copy leaves the caller's own buffer in place; a
        # gate that only checked the ioctl's return code would accept it.
        claimed = classify_transcript(
            self.transcript(1, "2", caps_bytes=0), expected_3d=True
        )
        self.assertFalse(claimed.passed)
        self.assertIn("capability bytes were delivered", claimed.reason)

        # And the inverse: a host without 3D that still hands over a blob.
        unexpected = classify_transcript(
            self.transcript(0, "0", caps_bytes=512), expected_3d=False
        )
        self.assertFalse(unexpected.passed)
        self.assertIn("capability bytes were delivered", unexpected.reason)

    def test_rejects_a_missing_report_or_unordered_markers(self) -> None:
        self.assertFalse(
            classify_transcript(READY_MARKER + b"\n", expected_3d=True).passed
        )
        # A report with no ready marker after it never reached the end.
        self.assertFalse(
            classify_transcript(self.transcript(1, "2")[:-len(READY_MARKER) - 1],
                                expected_3d=True).passed
        )
        # A context marker before the capability blob is not the order the
        # probe walks, so the evidence would not describe one run.
        reordered = b"\n".join(
            (
                b"DRM_VIRGL_PARAM 3d=1 capsets=0x2",
                CONTEXT_MARKER,
                b"DRM_VIRGL_CAPS PASS caps_bytes=512",
                READY_MARKER,
            )
        )
        self.assertFalse(classify_transcript(reordered, expected_3d=True).passed)

    def test_surfaces_the_guests_own_diagnosis(self) -> None:
        transcript = b"DRM_VIRGL_FAIL stage=3d-features errno=22\n"
        result = classify_transcript(transcript, expected_3d=True)
        self.assertFalse(result.passed)
        self.assertIn("3d-features", result.reason)
        self.assertIn("errno=22", result.reason)

    def test_scans_the_full_bounded_transcript_for_fatal_output(self) -> None:
        for fatal in (b"Uncaught panic", b"unexpected exception"):
            with self.subTest(fatal=fatal):
                transcript = self.transcript(1, "2") + fatal
                self.assertFalse(classify_transcript(transcript, expected_3d=True).passed)

        with self.assertRaises(ValueError):
            classify_transcript(b"x" * (MAX_TRANSCRIPT_BYTES + 1), expected_3d=True)

    def test_runtime_publishes_the_final_result_and_stale_success_does_not_survive(
        self,
    ) -> None:
        for runner_result in (True, False):
            with self.subTest(runner_result=runner_result):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    output = Path(temporary_directory) / "evidence"
                    output.mkdir()
                    (output / "result.json").write_text('{"passed": true}\n')

                    def fake_runner(**arguments: object) -> object:
                        Path(arguments["serial_log"]).write_bytes(self.transcript(1, "2"))
                        return type("BaseResult", (), {"passed": runner_result})()

                    result = run_virgl_param_gate(
                        VirglParamGateConfig(
                            uboot=Path("/inputs/u-boot"),
                            boot_disk=Path("/inputs/boot.ext4"),
                            manifest=Path("/inputs/artifacts.json"),
                            output_directory=output,
                        ),
                        runner=fake_runner,
                    )
                    published = json.loads((output / "result.json").read_text())
                    self.assertEqual(result.passed, runner_result)
                    self.assertEqual(published["passed"], runner_result)

    def test_runtime_failure_never_leaves_stale_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "evidence"
            output.mkdir()
            (output / "result.json").write_text('{"passed": true}\n')

            def failing_runner(**_arguments: object) -> object:
                raise RuntimeError("launch failed")

            result = run_virgl_param_gate(
                VirglParamGateConfig(
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


class DrmVirglGuestProbeTests(unittest.TestCase):
    SELF_TEST_CASE_NAMES = (
        "valid",
        "no-3d",
        "unknown-accepted",
        "known-refused",
        "caps-and-context",
        "caps-empty",
        "context-twice-allowed",
        "no-3d-refuses-caps",
        "no-3d-caps-succeed",
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = TOOLS / "drm/virgl_param_gate_init.c"

    def compile_probe(
        self, output: Path, define: str | None
    ) -> subprocess.CompletedProcess[str]:
        command = ["cc", "-std=c11", "-O2", "-static", "-Wall", "-Wextra", "-Werror"]
        if define is not None:
            command.append(f"-D{define}")
        command += [os.fspath(self.source), "-o", os.fspath(output)]
        return subprocess.run(command, capture_output=True, check=False, text=True)

    def test_native_self_test_covers_both_report_outcomes_and_the_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            binary = Path(temporary_directory) / "virgl-self-test"
            result = self.compile_probe(binary, "DRM_VIRGL_GATE_SELF_TEST")
            self.assertEqual(result.returncode, 0, result.stderr)
            for case in self.SELF_TEST_CASE_NAMES:
                with self.subTest(case=case):
                    run = subprocess.run(
                        [binary, case], capture_output=True, check=False, text=True
                    )
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    self.assertEqual(
                        run.stdout.splitlines()[-1],
                        f"DRM_VIRGL_SELF_TEST PASS case={case}",
                    )

    def test_self_test_rejects_an_unknown_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            binary = Path(temporary_directory) / "virgl-self-test"
            result = self.compile_probe(binary, "DRM_VIRGL_GATE_SELF_TEST")
            self.assertEqual(result.returncode, 0, result.stderr)
            run = subprocess.run(
                [binary, "not-a-case"], capture_output=True, check=False, text=True
            )
            self.assertEqual(run.returncode, 2)

    def test_lifecycle_publishes_the_report_then_ready_and_holds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            binary = Path(temporary_directory) / "virgl-lifecycle"
            result = self.compile_probe(binary, "DRM_VIRGL_GATE_LIFECYCLE_TEST")
            self.assertEqual(result.returncode, 0, result.stderr)
            with subprocess.Popen(
                [binary], stdout=subprocess.PIPE, stderr=subprocess.PIPE
            ) as process:
                try:
                    assert process.stdout is not None
                    output = b""
                    deadline = time.monotonic() + 2.0
                    while output.count(b"\n") < 2:
                        readable, _, _ = select.select(
                            [process.stdout], [], [], max(0.0, deadline - time.monotonic())
                        )
                        self.assertTrue(readable, "virgl marker timed out")
                        output += os.read(process.stdout.fileno(), 4096)
                    lines = output.decode().splitlines()
                    self.assertEqual(lines[-1], READY_MARKER.decode())
                    # The lifecycle fake is a virgl host, so the report the
                    # gate consumes is the one the classifier accepts.
                    self.assertTrue(
                        classify_transcript(output, expected_3d=True).passed
                    )
                    self.assertIsNone(process.poll())
                finally:
                    process.terminate()
                    process.wait(timeout=2.0)


class DrmVirglBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.builder = TOOLS / "drm/build_virgl_param_gate.sh"

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
            first = directory / "first virgl.cpio.gz"
            second = directory / "second virgl.cpio.gz"
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
            self.assertEqual(
                parse_newc(gzip.decompress(first.read_bytes())),
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
            output = Path(temporary_directory) / "virgl.cpio.gz"
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
