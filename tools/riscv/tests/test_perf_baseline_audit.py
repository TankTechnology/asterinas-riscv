# SPDX-License-Identifier: MPL-2.0

import json
import tempfile
import unittest
from pathlib import Path

from tools.riscv.perf.audit_baseline import audit


class BaselineAuditTests(unittest.TestCase):
    def test_partial_stacked_matrix_is_not_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("fio.json", "sqlite.json", "net.json", "ops.json",
                         "hack.json", "sch.json"):
                (root / name).write_text("{}")
            manifest = {
                "comparison_status": "stacked_network_comparison_complete",
                "stacked_ext4_comparison": "fio.json",
                "stacked_ext4_sqlite_comparison": "sqlite.json",
                "stacked_network_comparison": "net.json",
                "stacked_common_ops_comparison": "ops.json",
                "stacked_hackbench_comparison": "hack.json",
                "stacked_schbench_comparison": "sch.json",
                "release": True,
                "qemu": "fixed",
                "samples": 5,
                "warmups": 1,
            }
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest))
            result = audit(path)
            self.assertFalse(result["complete"])
            self.assertFalse(result["checks"]["lmbench"]["present"])
            self.assertFalse(result["checks"]["main_ext4"]["present"])

    def test_complete_shape_requires_lmbench_and_main_ext4(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = {
                "stacked_ext4_comparison": "fio.json",
                "stacked_ext4_sqlite_comparison": "sqlite.json",
                "stacked_network_comparison": "net.json",
                "stacked_common_ops_comparison": "ops.json",
                "stacked_hackbench_comparison": "hack.json",
                "stacked_schbench_comparison": "sch.json",
            }
            for name in names.values():
                (root / name).write_text("{}")
            (root / "lmbench-debian-main-comparison.json").write_text("{}")
            manifest = {
                "comparison_status": "main_ext4_baseline_complete",
                **names,
                "release": True,
                "qemu": "fixed",
                "samples": 5,
                "warmups": 1,
            }
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest))
            self.assertTrue(audit(path)["complete"])


if __name__ == "__main__":
    unittest.main()
