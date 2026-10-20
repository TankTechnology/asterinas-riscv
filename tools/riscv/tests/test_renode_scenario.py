# SPDX-License-Identifier: MPL-2.0

"""Host checks for the Renode-compatible EIC7700 scenario matrix."""

from pathlib import Path
import unittest

from tools.riscv.renode.eic7700_scenario import (
    FAULTS,
    all_fault_combinations,
    run_scenario,
)


class RenodeScenarioTests(unittest.TestCase):
    def test_matrix_covers_all_fault_combinations_and_recovers(self):
        scenarios = list(all_fault_combinations())
        self.assertEqual(len(scenarios), 64)
        for faults in scenarios:
            result = run_scenario(faults)
            self.assertLessEqual(result["descriptor_submissions"], 1)
            self.assertLessEqual(result["irq_attempts"], 1)
            self.assertEqual(
                result["unavailable"],
                bool({"clock-gated", "reset-asserted", "mmio-timeout"} & faults),
            )

    def test_faults_stop_downstream_side_effects(self):
        blocked = run_scenario(("clock-gated",))
        self.assertEqual(blocked["mdio_reads"], 0)
        self.assertEqual(blocked["descriptor_submissions"], 0)
        self.assertEqual(blocked["irq_attempts"], 0)

        phy_absent = run_scenario(("phy-absent",))
        self.assertEqual(phy_absent["mdio_reads"], 1)
        self.assertEqual(phy_absent["descriptor_submissions"], 0)
        self.assertEqual(phy_absent["irq_attempts"], 0)

        descriptor_fault = run_scenario(("descriptor-unreachable",))
        self.assertEqual(descriptor_fault["descriptor_submissions"], 1)
        self.assertEqual(descriptor_fault["irq_attempts"], 0)

        irq_fault = run_scenario(("irq-drop",))
        self.assertEqual(irq_fault["descriptor_submissions"], 1)
        self.assertEqual(irq_fault["irq_attempts"], 1)
        self.assertFalse(irq_fault["irq_seen"])

    def test_artifacts_use_the_shared_fault_names_and_control_register(self):
        root = Path(__file__).parents[3] / "tools/riscv/renode"
        resc = (root / "eic7700_fault_matrix.resc").read_text()
        repl = (root / "eic7700_fault_matrix.repl").read_text()
        peripheral = (root / "eic7700_python_peripheral.py").read_text()
        self.assertIn("0x501ffff0", resc)
        self.assertNotIn("/mnt/shared/", resc + repl)
        self.assertEqual(resc.count("sysbus ReadDoubleWord"), 4)
        self.assertIn("Python.PythonPeripheral", repl)
        self.assertIn("script: '''", repl)
        self.assertNotIn("filename:", repl)
        self.assertIn("CONTROL = 0x1FFFF0", peripheral)
        self.assertIn("faults = 0", peripheral)
        self.assertNotIn("self.faults", peripheral)
        self.assertTrue(all(name in (root / "eic7700_scenario.py").read_text() for name in FAULTS))


if __name__ == "__main__":
    unittest.main()
