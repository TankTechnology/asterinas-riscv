#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Tests for tools/riscv/megrez_dtb_check.py.

The fixtures are compiled from DTS text with `dtc`, because the point of the
tool is to read a real device tree blob; the tests then assert on the findings,
not on the parser's internal shape.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.riscv import megrez_dtb_check as check

DTC = shutil.which("dtc")

BOARD_DTS = """/dts-v1/;

/ {
    #address-cells = <2>;
    #size-cells = <2>;
    compatible = "eswin,eic7700";
    model = "Milk-V Megrez";

    chosen {
        stdout-path = "/soc/serial@50440000";
        linux,initrd-start = <0x0 0x83000000>;
        linux,initrd-end = <0x0 0x8f000000>;
    };

    memory@80000000 {
        device_type = "memory";
        reg = <0x0 0x80000000 0x0 0x80000000>;
    };

    cpus {
        #address-cells = <1>;
        #size-cells = <0>;
        timebase-frequency = <0x1dcd6500>;

        cpu@0 {
            device_type = "cpu";
            reg = <0>;
            mmu-type = "riscv,sv48";
            riscv,isa = "rv64imafdc";
            interrupt-controller {
                compatible = "riscv,cpu-intc";
                #interrupt-cells = <1>;
                interrupt-controller;
            };
        };

        cpu@1 {
            device_type = "cpu";
            reg = <1>;
            mmu-type = "riscv,sv48";
            riscv,isa = "rv64imafdc";
            interrupt-controller {
                compatible = "riscv,cpu-intc";
                #interrupt-cells = <1>;
                interrupt-controller;
            };
        };
    };

    syscrg: clock-controller@51800000 {
        compatible = "eswin,syscrg";
        reg = <0x0 0x51800000 0x0 0x10000>;
        #clock-cells = <1>;
        phandle = <0x1>;
    };

    axi_config: axi-config {
        snps,blen = <0 0 0 0 16 8 4>;
        snps,rd_osr_lmt = <2>;
        snps,wr_osr_lmt = <2>;
        phandle = <0x2>;
    };

    soc {
        #address-cells = <2>;
        #size-cells = <2>;
        ranges;

        serial@50440000 {
            compatible = "snps,dw-apb-uart";
            reg = <0x0 0x50440000 0x0 0x10000>;
        };

        interrupt-controller@c000000 {
            compatible = "sifive,plic-1.0.0", "riscv,plic0";
            reg = <0x0 0xc000000 0x0 0x4000000>;
            riscv,ndev = <95>;
            interrupt-controller;
            #interrupt-cells = <1>;
        };

        mmc@50450000 {
            compatible = "eswin,sdhci-sdio";
            reg = <0x0 0x50450000 0x0 0x10000>;
            eswin,syscrg_csr = <0x1>;
        };

        ethernet@50460000 {
            compatible = "snps,dwmac-5.10a";
            reg = <0x0 0x50460000 0x0 0x10000>;
            snps,axi-config = <0x2>;
            snps,blen = <0 0 0 0 16 8 4>;
        };

        pci@30000000 {
            compatible = "pci-host-ecam-generic";
            reg = <0x0 0x30000000 0x0 0x10000000>;
        };

        framebuffer@88000000 {
            compatible = "simple-framebuffer";
            reg = <0x0 0x88000000 0x0 0x800000>;
        };
    };
};
"""


def compile_dts(text: str, directory: Path) -> Path:
    source = directory / "board.dts"
    output = directory / "board.dtb"
    source.write_text(text)
    subprocess.run([DTC, "-I", "dts", "-O", "dtb", "-o", str(output), str(source)],
                   check=True, capture_output=True)
    return output


@unittest.skipUnless(DTC, "dtc is required to build the fixtures")
class MegrezDtbCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def findings(self, text: str) -> list[check.Finding]:
        tree = check.load_tree(compile_dts(text, self.tmp))
        checker = check.Checker(tree)
        checker.check_cpus()
        checker.check_memory()
        checker.check_timebase()
        checker.check_chosen()
        checker.check_interrupts()
        checker.check_devices()
        return checker.findings

    def levels(self, text: str) -> dict[str, str]:
        return {finding.item: finding.level for finding in self.findings(text)}

    def test_complete_board_tree_has_no_failures(self) -> None:
        findings = self.findings(BOARD_DTS)
        failures = [f for f in findings if f.level == "fail"]
        self.assertEqual(failures, [], [f.detail for f in failures])
        levels = {f.item: f.level for f in findings}
        self.assertEqual(levels["stdout-path"], "ok")
        self.assertEqual(levels["mmc"], "ok")
        self.assertEqual(levels["gmac"], "ok")
        self.assertEqual(levels["pci"], "ok")
        self.assertEqual(levels["framebuffer"], "ok")
        self.assertEqual(levels["initrd"], "ok")

    def test_missing_stdout_path_is_fatal(self) -> None:
        text = BOARD_DTS.replace('        stdout-path = "/soc/serial@50440000";\n', "")
        self.assertEqual(self.levels(text)["stdout-path"], "fail")

    def test_unknown_uart_compatible_is_fatal(self) -> None:
        text = BOARD_DTS.replace('compatible = "snps,dw-apb-uart";',
                                 'compatible = "vendor,soc-uart";')
        self.assertEqual(self.levels(text)["stdout-path"], "fail")

    def test_sifive_uart_is_accepted(self) -> None:
        text = BOARD_DTS.replace('compatible = "snps,dw-apb-uart";',
                                 'compatible = "sifive,uart0";')
        self.assertEqual(self.levels(text)["stdout-path"], "ok")

    def test_missing_timebase_is_fatal(self) -> None:
        text = BOARD_DTS.replace("        timebase-frequency = <0x1dcd6500>;\n", "")
        self.assertEqual(self.levels(text)["timebase"], "fail")

    def test_missing_memory_is_fatal(self) -> None:
        text = BOARD_DTS.replace("""    memory@80000000 {
        device_type = "memory";
        reg = <0x0 0x80000000 0x0 0x80000000>;
    };
""", "")
        self.assertEqual(self.levels(text)["memory"], "fail")

    def test_duplicate_hart_is_fatal(self) -> None:
        text = BOARD_DTS.replace("            reg = <1>;", "            reg = <0>;")
        self.assertEqual(self.levels(text)["cpus"], "fail")

    def test_half_an_initrd_range_is_fatal(self) -> None:
        text = BOARD_DTS.replace("        linux,initrd-end = <0x0 0x8f000000>;\n", "")
        self.assertEqual(self.levels(text)["initrd"], "fail")

    def test_missing_initrd_range_is_only_a_warning(self) -> None:
        text = BOARD_DTS.replace("        linux,initrd-start = <0x0 0x83000000>;\n", "")
        text = text.replace("        linux,initrd-end = <0x0 0x8f000000>;\n", "")
        self.assertEqual(self.levels(text)["initrd"], "warn")

    def test_mmc_without_syscrg_handle_is_fatal(self) -> None:
        text = BOARD_DTS.replace("            eswin,syscrg_csr = <0x1>;\n", "")
        self.assertEqual(self.levels(text)["mmc"], "fail")

    def test_gmac_without_axi_config_is_fatal(self) -> None:
        text = BOARD_DTS.replace("            snps,axi-config = <0x2>;\n", "")
        self.assertEqual(self.levels(text)["gmac"], "fail")

    def test_absent_devices_are_warnings_not_failures(self) -> None:
        text = BOARD_DTS.replace('compatible = "eswin,sdhci-sdio";', 'compatible = "x,mmc";')
        text = text.replace('compatible = "snps,dwmac-5.10a";', 'compatible = "x,gmac";')
        levels = self.levels(text)
        self.assertEqual(levels["mmc"], "warn")
        self.assertEqual(levels["gmac"], "warn")

    def test_cli_json_reports_ok_and_findings(self) -> None:
        dtb = compile_dts(BOARD_DTS, self.tmp)
        completed = subprocess.run(
            [sys.executable, "tools/riscv/megrez_dtb_check.py", str(dtb), "--json"],
            check=True, capture_output=True, text=True,
        )
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        items = {finding["item"] for finding in payload["findings"]}
        self.assertIn("stdout-path", items)

    def test_cli_exit_status_follows_failures(self) -> None:
        text = BOARD_DTS.replace('        stdout-path = "/soc/serial@50440000";\n', "")
        dtb = compile_dts(text, self.tmp)
        completed = subprocess.run(
            [sys.executable, "tools/riscv/megrez_dtb_check.py", str(dtb)],
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("stdout-path", completed.stdout)


if __name__ == "__main__":
    unittest.main()
