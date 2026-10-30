#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the aggregate QEMU product gate."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tools.riscv.debian import product_gate as gate


class CommandTests(unittest.TestCase):
    def test_direct_command_carries_optional_dsh_image(self) -> None:
        command = gate.build_direct_command(
            Path("/k.Image"),
            Path("/root.ext2"),
            dsh_image=Path("/dsh.ext2"),
            rootfstype="ext4",
            log=Path("/log"),
        )

        self.assertIn(str(gate.DIRECT_GATE), command)
        self.assertEqual(command[command.index("--dsh-image") + 1], "/dsh.ext2")
        self.assertEqual(command[command.index("--rootfstype") + 1], "ext4")
        self.assertEqual(command[command.index("--log") + 1], "/log")

    def test_ext4_command_carries_the_fault_stage_selection(self) -> None:
        command = gate.build_ext4_command(
            Path("/k.Image"),
            Path("/root.ext4"),
            log_dir=Path("/logs"),
            fault_stages="2,4",
        )

        self.assertIn(str(gate.EXT4_GATE), command)
        self.assertEqual(command[command.index("--fault-stages") + 1], "2,4")
        self.assertEqual(command[command.index("--log-dir") + 1], "/logs")


class ComponentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.receipt = Path(self.directory.name) / "receipt.json"
        self.log = Path(self.directory.name) / "receipt.log"

    def run_code(self, code: str, *, timeout: float = 10.0) -> dict[str, object]:
        return gate.run_component(
            "direct",
            [sys.executable, "-c", code],
            receipt=self.receipt,
            log=self.log,
            timeout=timeout,
        )

    def test_passing_receipt_is_returned_and_saved_as_json(self) -> None:
        result = self.run_code(
            "import json; print(json.dumps({'status': 'pass'}))"
        )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(json.loads(self.receipt.read_text()), {"status": "pass"})
        self.assertIn("status: completed", self.log.read_text())

    def test_failed_component_raises_and_saves_json_receipt(self) -> None:
        code = (
            "import json, sys; "
            "print(json.dumps({'status': 'fail', 'error': 'serial timeout'})); "
            "sys.exit(1)"
        )

        with self.assertRaisesRegex(gate.ProductGateError, "serial timeout"):
            self.run_code(code)
        self.assertEqual(
            json.loads(self.receipt.read_text())["status"], "fail"
        )
        self.assertIn("serial timeout", self.log.read_text())

    def test_non_json_output_raises_without_a_receipt(self) -> None:
        with self.assertRaisesRegex(gate.ProductGateError, "JSON receipt"):
            self.run_code("print('not json')")
        self.assertFalse(self.receipt.exists())
        self.assertIn("not json", self.log.read_text())

    def test_timeout_raises_and_records_the_timeout(self) -> None:
        with self.assertRaisesRegex(gate.ProductGateError, "timed out"):
            self.run_code("import time; time.sleep(30)", timeout=0.2)
        self.assertIn("status: timed out", self.log.read_text())


class InputTests(unittest.TestCase):
    def test_defaults_cover_the_active_artifacts(self) -> None:
        args = gate.parse_args(["--kernel", "k", "--rootfs", "r"])

        self.assertIsNone(args.ext4_rootfs)
        self.assertIsNone(args.dsh_image)
        self.assertEqual(args.fault_stages, "1,2,3,4")
        self.assertEqual(args.timeout, 1800.0)

    def test_missing_kernel_is_rejected_before_any_gate(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            root = Path(temporary)
            gate.main(
                [
                    "--kernel",
                    str(root / "missing.Image"),
                    "--rootfs",
                    str(root / "rootfs.ext2"),
                ]
            )
        self.assertEqual(raised.exception.code, 2)


class ProductRunTests(unittest.TestCase):
    def test_direct_and_ext4_components_write_a_pass_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kernel = root / "kernel.Image"
            rootfs = root / "rootfs.ext2"
            ext4 = root / "rootfs.ext4"
            for path in (kernel, rootfs, ext4):
                path.write_bytes(b"artifact")
            log_dir = root / "logs"

            with (
                mock.patch.object(gate.shutil, "which", return_value="/bin/tool"),
                mock.patch.object(
                    gate,
                    "run_component",
                    side_effect=[
                        {"status": "pass", "gate": "direct"},
                        {"status": "pass", "gate": "ext4"},
                    ],
                ) as run_component,
            ):
                with redirect_stdout(io.StringIO()):
                    code = gate.main(
                        [
                            "--kernel",
                            str(kernel),
                            "--rootfs",
                            str(rootfs),
                            "--ext4-rootfs",
                            str(ext4),
                            "--log-dir",
                            str(log_dir),
                        ]
                    )

            self.assertEqual(code, 0)
            self.assertEqual(run_component.call_count, 2)
            result = json.loads((log_dir / "result.json").read_text())
            self.assertEqual(result["status"], "pass")
            self.assertEqual(set(result["components"]), {"direct", "ext4"})


class MakefileRecipeTests(unittest.TestCase):
    def test_optional_image_paths_with_spaces_stay_single_arguments(self) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_python = root / "python3"
            fake_python.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS_FILE"\n',
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            args_file = root / "args.txt"

            kernel = root / "kernel.Image"
            rootfs = root / "tool chain rootfs.ext2"
            ext4 = root / "my images/browser.ext4"
            dsh = root / "dsh repo.ext2"
            for path in (kernel, rootfs, ext4, dsh):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"artifact")
            log_dir = root / "gate logs"

            environment = os.environ.copy()
            environment["PATH"] = f"{root}{os.pathsep}{environment['PATH']}"
            environment["ARGS_FILE"] = str(args_file)
            completed = subprocess.run(
                [
                    "make",
                    "-s",
                    "test_riscv_debian_product",
                    f"DEBIAN_PRODUCT_KERNEL={kernel}",
                    f"DEBIAN_PRODUCT_ROOTFS={rootfs}",
                    f"DEBIAN_PRODUCT_EXT4_ROOTFS={ext4}",
                    f"DEBIAN_PRODUCT_DSH_IMAGE={dsh}",
                    "DEBIAN_PRODUCT_DIRECT_ROOTFSTYPE=ext4",
                    f"DEBIAN_PRODUCT_LOG_DIR={log_dir}",
                    "DEBIAN_PRODUCT_FAULT_STAGES=1,2",
                ],
                cwd=repo_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            arguments = args_file.read_text(encoding="utf-8").splitlines()

        def value_after(flag: str) -> str:
            return arguments[arguments.index(flag) + 1]

        self.assertEqual(value_after("--kernel"), str(kernel))
        self.assertEqual(value_after("--rootfs"), str(rootfs))
        self.assertEqual(value_after("--ext4-rootfs"), str(ext4))
        self.assertEqual(value_after("--dsh-image"), str(dsh))
        self.assertEqual(value_after("--direct-rootfstype"), "ext4")
        self.assertEqual(value_after("--log-dir"), str(log_dir))
        self.assertEqual(value_after("--fault-stages"), "1,2")


if __name__ == "__main__":
    unittest.main()
