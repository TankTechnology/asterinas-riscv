#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import gzip
import os
import stat
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]


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


class DrmFirmwareGuestProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = TOOLS / "drm/firmware_gate_init.c"

    def test_native_self_test_covers_success_and_migration_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            binary = Path(temporary_directory) / "firmware-self-test"
            compile_result = subprocess.run(
                [
                    "cc",
                    "-std=c11",
                    "-O2",
                    "-static",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-DDRM_FIRMWARE_GATE_SELF_TEST",
                    os.fspath(self.source),
                    "-o",
                    os.fspath(binary),
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            expected = {
                "valid": "ok",
                "bad-driver": "version",
                "render-node": "render-node",
                "bad-cap": "capability",
                "bad-mode": "connector",
                "ioctl-error": "set-crtc",
            }
            for case, stage in expected.items():
                with self.subTest(case=case):
                    result = subprocess.run(
                        [binary, case], capture_output=True, check=False, text=True
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        result.stdout,
                        f"DRM_FIRMWARE_SELF_TEST PASS case={case} stage={stage}\n",
                    )

    def test_probe_has_closed_megrez_and_drm_contract(self) -> None:
        source = self.source.read_text()
        for contract in (
            'strcmp(driver, "simpledrm")',
            "DRM_CAP_DUMB_BUFFER, 1",
            "DRM_CAP_DUMB_PREFER_SHADOW, 1",
            "DRM_CAP_CURSOR_WIDTH, 0",
            "DRM_CAP_CURSOR_HEIGHT, 0",
            "DRM_IOCTL_MODE_CREATE_DUMB",
            "DRM_IOCTL_MODE_MAP_DUMB",
            "DRM_IOCTL_MODE_ADDFB2",
            "DRM_IOCTL_MODE_SETCRTC",
            "DRM_IOCTL_MODE_DIRTYFB",
            "DRM_IOCTL_MODE_PAGE_FLIP",
            "MEGREZ_WIDTH 1920U",
            "MEGREZ_HEIGHT 1080U",
            'stat("/dev/dri/renderD128"',
            'open("/dev/ttyS0"',
            "ioctl(fd, KDSETMODE, KD_GRAPHICS)",
            'stage=setcrtc pattern=A ioctl=pass',
            'stage=page-flip pattern=B ioctl=pass',
            'stage=dirtyfb pattern=C ioctl=pass',
            'publish_marker("ASTERINAS_DRM_FIRMWARE_R1_READY")',
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, source)

        workflow = source[source.index("static enum gate_stage run_probe") :]
        self.assertLess(
            workflow.index("take_graphics_ownership()"),
            workflow.index("DRM_IOCTL_MODE_SETCRTC, &set"),
        )
        self.assertLess(
            workflow.index("DRM_IOCTL_MODE_SETCRTC, &set"),
            workflow.index("DRM_IOCTL_MODE_PAGE_FLIP, &flip"),
        )
        self.assertLess(
            workflow.index("DRM_IOCTL_MODE_PAGE_FLIP, &flip"),
            workflow.index("DRM_IOCTL_MODE_DIRTYFB, &dirty"),
        )

    def test_dirty_request_is_validated_before_the_active_scanout_check(self) -> None:
        drm_module = (
            TOOLS.parents[1] / "kernel/src/device/drm/mod.rs"
        ).read_text()
        dirty_arm = drm_module[drm_module.index("cmd @ ModeDirtyFb =>") :]
        self.assertLess(
            dirty_arm.index("kms::validate_dirty_fb(&framebuffer, req)?"),
            dirty_arm.index("kms_state.scanout_matches"),
        )


class DrmFirmwareBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.builder = TOOLS / "drm/build_firmware_gate.sh"

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

    def test_builder_is_deterministic_and_contains_only_the_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            compiler, elf = self.make_fake_compiler(directory)
            first = directory / "first firmware.cpio.gz"
            second = directory / "second firmware.cpio.gz"
            environment = os.environ | {
                "RISC_V_CC": os.fspath(compiler),
                "FAKE_RISCV_ELF": os.fspath(elf),
            }
            for output in (first, second):
                result = subprocess.run(
                    [self.builder, output],
                    env=environment,
                    capture_output=True,
                    check=False,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
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
            output = Path(temporary_directory) / "firmware.cpio.gz"
            output.write_bytes(b"old archive")
            result = subprocess.run(
                [self.builder, output],
                env=os.environ | {"RISC_V_CC": "missing-riscv-compiler"},
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), b"old archive")


if __name__ == "__main__":
    unittest.main()
