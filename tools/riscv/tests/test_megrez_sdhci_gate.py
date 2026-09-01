# SPDX-License-Identifier: MPL-2.0

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.riscv.debian.rootfs.megrez_installer import parse_newc

from tools.riscv.megrez_sdhci_gate import (
    MAX_TRANSCRIPT_BYTES,
    READ_PROBE_BYTES,
    classify,
    main,
    publish,
)


BUFFER = "[mmc] SDMA buffer cpu=0xfff00000 device=0xfff00000 bytes=524288"
CONTROLLER = "[mmc] controller 0x50460000 irq=81 sdma boundary=524288"
CARD = "[mmc] SDHC rca=43690 sectors=249737216 sector0=55aa"
BLOCK = "[mmc] mmcblk0 registered read-only"
READ_START = "MEGREZ_SDHCI_READ_START bytes=33554432 uptime=42.125"
READ_PASS = (
    "MEGREZ_SDHCI_READ_PASS bytes=33554432 crc32=5f85f90e start=42.125 end=42.375"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PROBE_SOURCE = REPOSITORY_ROOT / "tools/riscv/megrez_sdhci_probe_init.c"
PROBE_BUILDER = REPOSITORY_ROOT / "tools/riscv/build_megrez_sdhci_probe.sh"


class MegrezSdhciGateTests(unittest.TestCase):
    def transcript(self, *lines: str) -> bytes:
        return ("\n".join(lines) + "\n").encode()

    def test_accepts_one_complete_ordered_read_only_run(self):
        result = classify(self.transcript(BUFFER, CONTROLLER, CARD, BLOCK))
        self.assertTrue(result.passed)
        self.assertEqual(result.sectors, 249737216)

    def test_rejects_missing_duplicate_or_out_of_order_markers(self):
        cases = {
            "missing": self.transcript(BUFFER, CONTROLLER, CARD),
            "duplicate": self.transcript(BUFFER, CONTROLLER, CARD, CARD, BLOCK),
            "out-of-order": self.transcript(BUFFER, CONTROLLER, BLOCK, CARD),
            "translated-address": self.transcript(
                BUFFER.replace("device=0xfff00000", "device=0x5ff00000"),
                CONTROLLER,
                CARD,
                BLOCK,
            ),
            "out-of-window-address": self.transcript(
                BUFFER.replace("0xfff00000", "0xbff80000"),
                CONTROLLER,
                CARD,
                BLOCK,
            ),
            "unaligned-address": self.transcript(
                BUFFER.replace("0xfff00000", "0xfff10000"),
                CONTROLLER,
                CARD,
                BLOCK,
            ),
        }
        for name, transcript in cases.items():
            with self.subTest(name=name):
                self.assertFalse(classify(transcript).passed)

    def test_rejects_fatal_or_write_enabled_evidence(self):
        for line in [
            "Uncaught panic: block read failed",
            "fatal exception",
            "[mmc] probe failed at host-handoff-or-card",
            "[mmc] mmcblk0 write-enabled",
            "[mmc] controller 0x50460000 irq=81 bounded-pio-fallback reason=Unsupported",
        ]:
            with self.subTest(line=line):
                result = classify(
                    self.transcript(BUFFER, CONTROLLER, CARD, BLOCK, line)
                )
                self.assertFalse(result.passed)

    def test_rejects_invalid_capacity_and_oversized_transcript(self):
        self.assertFalse(
            classify(
                self.transcript(BUFFER, CONTROLLER, "[mmc] SDHC rca=1 sectors=0", BLOCK)
            ).passed
        )
        self.assertFalse(classify(b"x" * (MAX_TRANSCRIPT_BYTES + 1)).passed)

    def test_publishes_complete_log_and_atomic_json_result(self):
        transcript = self.transcript(BUFFER, CONTROLLER, CARD, BLOCK)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence"
            result = publish(transcript, output)
            self.assertTrue(result.passed)
            self.assertEqual((output / "serial.log").read_bytes(), transcript)
            payload = json.loads((output / "result.json").read_text())
            self.assertTrue(payload["passed"])
            self.assertEqual(payload["sectors"], 249737216)
            self.assertNotIn("partition_sha256", payload)
            self.assertEqual(list(output.glob(".*.tmp")), [])

    def test_accepts_exact_bounded_read_against_uboot_crc(self):
        transcript = self.transcript(
            BUFFER, CONTROLLER, CARD, BLOCK, READ_START, READ_PASS
        )

        result = classify(transcript, expected_crc32="5f85f90e")

        self.assertTrue(result.passed)
        self.assertEqual(result.read_bytes, READ_PROBE_BYTES)
        self.assertEqual(result.read_crc32, "5f85f90e")
        self.assertEqual(result.elapsed_seconds, 0.25)

    def test_rejects_wrong_crc_size_order_or_uptime(self):
        cases = {
            "wrong-crc": READ_PASS.replace("5f85f90e", "00000000"),
            "wrong-size": READ_PASS.replace("33554432", "1048576"),
            "backwards-time": READ_PASS.replace("end=42.375", "end=42.000"),
            "start-mismatch": READ_PASS.replace("start=42.125", "start=41.000"),
        }
        for name, pass_marker in cases.items():
            with self.subTest(name=name):
                result = classify(
                    self.transcript(
                        BUFFER, CONTROLLER, CARD, BLOCK, READ_START, pass_marker
                    ),
                    expected_crc32="5f85f90e",
                )
                self.assertFalse(result.passed)

        out_of_order = self.transcript(
            BUFFER, CONTROLLER, CARD, BLOCK, READ_PASS, READ_START
        )
        self.assertFalse(classify(out_of_order, expected_crc32="5f85f90e").passed)

    def test_cli_binds_the_physical_log_to_the_uboot_crc(self):
        transcript = self.transcript(
            BUFFER, CONTROLLER, CARD, BLOCK, READ_START, READ_PASS
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "serial.log"
            output = root / "evidence"
            log.write_bytes(transcript)

            status = main(
                [
                    "--transcript",
                    str(log),
                    "--output-dir",
                    str(output),
                    "--expected-crc32",
                    "5f85f90e",
                ]
            )

            self.assertEqual(status, 0)
            result = json.loads((output / "result.json").read_text())
            self.assertEqual(result["read_crc32"], "5f85f90e")
            self.assertEqual(result["read_bytes"], READ_PROBE_BYTES)

    def test_boot_assembly_uses_the_compiled_paging_mode(self):
        boot = (REPOSITORY_ROOT / "ostd/src/arch/riscv/boot/mod.rs").read_text()
        smp = (REPOSITORY_ROOT / "ostd/src/arch/riscv/boot/smp.rs").read_text()
        bsp = (REPOSITORY_ROOT / "ostd/src/arch/riscv/boot/bsp_boot.S").read_text()
        ap = (REPOSITORY_ROOT / "ostd/src/arch/riscv/boot/ap_boot.S").read_text()

        self.assertIn('cfg!(feature = "riscv_sv39_mode")', boot)
        self.assertIn("SATP_MODE = const BOOT_SATP_MODE", boot)
        self.assertIn("SATP_MODE = const super::BOOT_SATP_MODE", smp)
        for assembly in (bsp, ap):
            self.assertIn("li", assembly)
            self.assertIn("{SATP_MODE}", assembly)
            self.assertNotIn("SATP_MODE_SV48", assembly)

    def test_native_probe_crc_self_test_uses_the_production_implementation(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "probe-self-test"
            compile_result = subprocess.run(
                [
                    "cc",
                    "-static",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-DSDHCI_PROBE_SELF_TEST",
                    "-DEXPECTED_CRC32=0x5f85f90eU",
                    str(PROBE_SOURCE),
                    "-o",
                    str(binary),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)

            result = subprocess.run(
                [binary], check=False, capture_output=True, text=True, timeout=2
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "SDHCI_PROBE_SELF_TEST_PASS\n")

    def test_builder_publishes_one_deterministic_bound_static_init(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.cpio"
            second = root / "second.cpio"
            environment = os.environ.copy()
            environment.update({"RISC_V_CC": "cc", "SOURCE_DATE_EPOCH": "1700000000"})
            command = [str(PROBE_BUILDER), "--expected-crc32", "5f85f90e"]

            for output in (first, second):
                result = subprocess.run(
                    [*command, output],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first.stat().st_mode & 0o777, 0o644)
            entries = parse_newc(first.read_bytes())
            self.assertEqual(tuple(entry.name for entry in entries), (".", "init"))
            self.assertEqual(entries[0].mode & 0o7777, 0o755)
            self.assertEqual(entries[1].mode & 0o7777, 0o755)
            self.assertTrue(entries[1].data.startswith(b"\x7fELF"))

            relative = subprocess.run(
                [*command, "nested/probe.cpio"],
                cwd=root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(relative.returncode, 0, relative.stderr)
            self.assertEqual(
                (root / "nested/probe.cpio").read_bytes(), first.read_bytes()
            )

            protected = root / "protected.cpio"
            protected.write_bytes(b"keep")
            bad = subprocess.run(
                [
                    str(PROBE_BUILDER),
                    "--expected-crc32",
                    "5F85F90E",
                    protected,
                ],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(bad.returncode, 0)
            self.assertEqual(protected.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
