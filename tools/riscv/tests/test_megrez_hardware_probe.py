# SPDX-License-Identifier: MPL-2.0

"""Contract tests for the read-only Megrez hardware probe runner."""

from pathlib import Path
import unittest

from tools.riscv.megrez_hardware_probe import (
    PROBE_SPECS,
    ProbeContractError,
    probe_menu_args,
    validate_prerequisites,
    classify_probe_timeout,
    validate_probe_evidence,
    validate_probe_timeout_evidence,
)


def evidence(
    device: str,
    reads: tuple[int, ...],
    *,
    status: str = "pass",
    failure_reason: str | None = None,
) -> str:
    spec = PROBE_SPECS[device]
    lines = [
        f"ASTERINAS_PROBE_BEGIN device={device}",
        f"ASTERINAS_PROBE_RESOURCE device={device} base=0x{spec.base:08x} size=0x{spec.size:x} irq={spec.irq}",
    ]
    if device.startswith("gmac-"):
        lines.append(f"ASTERINAS_PROBE_DT device={device} clocks=[3, 550] resets=[20, 7]")
    values = {0x10: 0x0220FE45, 0xC110: 0x00192004, 0xC120: 0x5533330B}
    if device.startswith("gmac-"):
        values = {0: 0, 0xB0: 8, 0x110: 0x1052, 0x120: 0x21008145, 0x1000: 0, 0x1008: 0}
    lines.extend(
        f"ASTERINAS_PROBE_READ device={device} offset=0x{offset:04x} value=0x{values[offset]:08x}"
        for offset in reads
    )
    if failure_reason is not None:
        lines.append(f"ASTERINAS_PROBE_FAIL device={device} reason={failure_reason}")
    else:
        lines.append(f"ASTERINAS_PROBE_{status.upper()} device={device}")
    lines.extend(
        [
            f"ASTERINAS_PROBE_DONE device={device} status={status}",
            "ASTERINAS_PROBE_AUTO_REBOOT",
            "OpenSBI v1.5",
            "U-Boot 2024.10",
        ]
    )
    return "\n".join(lines) + "\n"


def timeout_evidence(
    device: str, reads: tuple[int, ...] = (), attempts: tuple[int, ...] = ()
) -> str:
    spec = PROBE_SPECS[device]
    lines = [
        f"ASTERINAS_PROBE_BEGIN device={device}",
        f"ASTERINAS_PROBE_RESOURCE device={device} base=0x{spec.base:08x} size=0x{spec.size:x} irq={spec.irq}",
    ]
    if device.startswith("gmac-"):
        lines.append(
            f"ASTERINAS_PROBE_DT device={device} clocks=[3, 550] resets=[20, 7]"
        )
    lines.extend(
        f"ASTERINAS_PROBE_ATTEMPT device={device} offset=0x{offset:04x}"
        for offset in attempts
    )
    values = {0x10: 0x0220FE45, 0xC110: 0x00192004, 0xC120: 0x5533330B}
    if device.startswith("gmac-"):
        values = {0: 0, 0xB0: 8, 0x110: 0x1052, 0x120: 0x21008145, 0x1000: 0, 0x1008: 0}
    lines.extend(
        f"ASTERINAS_PROBE_READ device={device} offset=0x{offset:04x} value=0x{values[offset]:08x}"
        for offset in reads
    )
    lines.extend(["OpenSBI v1.5", "U-Boot 2024.10"])
    return "\n".join(lines) + "\n"


class HardwareProbeTests(unittest.TestCase):
    def test_pass_rejects_unavailable_and_wrong_identity_registers(self):
        cases = [
            ("gmac-1", "value=0x00001052", "value=0x00001051"),
            ("gmac-0", "value=0x00000000", "value=0xffffffff"),
            ("dwc3-0", "value=0x5533330b", "value=0x00000000"),
            ("dwc3-1", "value=0x5533330b", "value=0x1234330b"),
            ("dwc3-1", "value=0x0220fe45", "value=0x1ffffffff"),
        ]
        for device, old, new in cases:
            with self.subTest(device=device, value=new):
                text = evidence(device, PROBE_SPECS[device].whitelist).replace(old, new, 1)
                with self.assertRaisesRegex(ProbeContractError, "register value"):
                    validate_probe_evidence(text, device, run_id="3" * 32)

    def test_recovery_must_be_after_completion_in_firmware_order(self):
        text = evidence("gmac-0", PROBE_SPECS["gmac-0"].whitelist)
        for bad in [
            "OpenSBI v1.5\nU-Boot 2024.10\n" + text.replace("OpenSBI v1.5\nU-Boot 2024.10\n", ""),
            text.replace("OpenSBI v1.5\nU-Boot 2024.10", "U-Boot 2024.10\nOpenSBI v1.5"),
            text.replace("ASTERINAS_PROBE_AUTO_REBOOT\n", ""),
            text.replace("ASTERINAS_PROBE_AUTO_REBOOT\n", "", 1).replace(
                "ASTERINAS_PROBE_BEGIN", "ASTERINAS_PROBE_AUTO_REBOOT\nASTERINAS_PROBE_BEGIN", 1
            ),
        ]:
            with self.subTest(transcript=bad):
                with self.assertRaisesRegex(ProbeContractError, "recovery epoch"):
                    validate_probe_evidence(bad, "gmac-0", run_id="4" * 32)

    def test_read_markers_outside_probe_interval_are_rejected(self):
        text = evidence("gmac-0", PROBE_SPECS["gmac-0"].whitelist)
        read = "ASTERINAS_PROBE_READ device=gmac-0 offset=0x0110 value=0x00001052\n"
        for bad in [read + text.replace(read, ""), text.replace(read, "") + read]:
            with self.assertRaises(ProbeContractError):
                validate_probe_evidence(bad, "gmac-0", run_id="5" * 32)

    def test_registry_is_explicit_and_has_no_overlapping_devices(self):
        self.assertEqual(tuple(PROBE_SPECS), ("dwc3-0", "dwc3-1", "gmac-0", "gmac-1"))
        self.assertEqual(
            [PROBE_SPECS[name].base for name in PROBE_SPECS],
            [0x50480000, 0x50490000, 0x50400000, 0x50410000],
        )
        self.assertTrue(all(spec.size == 0x10000 for spec in PROBE_SPECS.values()))
        self.assertEqual(len({spec.irq for spec in PROBE_SPECS.values()}), 4)
        self.assertTrue(all(spec.whitelist == tuple(dict.fromkeys(spec.whitelist)) for spec in PROBE_SPECS.values()))

    def test_menu_args_select_one_device_and_never_enable_normal_io(self):
        for device in PROBE_SPECS:
            args = probe_menu_args(device)
            self.assertIn(f"asterinas.hardware_probe={device}", args)
            self.assertIn("asterinas.reboot_after=60", args)
            self.assertIn("--root-init=probe-auto", args)
            self.assertNotIn("usb_bulk_selftest", args)
            self.assertEqual(args.count("asterinas.hardware_probe="), 1)

    def test_valid_probe_evidence_requires_all_whitelisted_reads(self):
        for device, spec in PROBE_SPECS.items():
            result = validate_probe_evidence(evidence(device, spec.whitelist), device, run_id="a" * 32)
            self.assertEqual(result.device, device)
            self.assertEqual(result.status, "pass")
            self.assertTrue(result.recovered)
        self.assertEqual(
            validate_probe_evidence(
                evidence("dwc3-0", PROBE_SPECS["dwc3-0"].whitelist).replace("\n", "\r\n"),
                "dwc3-0",
                run_id="a" * 32,
        ).read_offsets,
            PROBE_SPECS["dwc3-0"].whitelist,
        )

    def test_platform_initialization_phase_is_part_of_probe_interval(self):
        text = evidence("gmac-1", PROBE_SPECS["gmac-1"].whitelist)
        text = text.replace(
            "ASTERINAS_PROBE_RESOURCE device=gmac-1",
            "ASTERINAS_PROBE_PHASE device=gmac-1 phase=platform-init\n"
            "ASTERINAS_PROBE_RESOURCE device=gmac-1",
            1,
        )
        result = validate_probe_evidence(text, "gmac-1", run_id="a" * 32)
        self.assertEqual(result.status, "pass")

    def test_probe_attempts_must_match_completed_mmio_reads(self):
        device = "gmac-1"
        text = evidence(device, PROBE_SPECS[device].whitelist)
        for offset in PROBE_SPECS[device].whitelist:
            marker = f"ASTERINAS_PROBE_READ device={device} offset=0x{offset:04x}"
            text = text.replace(
                marker,
                f"ASTERINAS_PROBE_ATTEMPT device={device} offset=0x{offset:04x}\n{marker}",
                1,
            )
        result = validate_probe_evidence(text, device, run_id="a" * 32)
        self.assertEqual(result.attempt_offsets, PROBE_SPECS[device].whitelist)
        bad = text.replace(
            f"ASTERINAS_PROBE_ATTEMPT device={device} offset=0x0000",
            f"ASTERINAS_PROBE_ATTEMPT device={device} offset=0x00b0",
            1,
        )
        with self.assertRaisesRegex(ProbeContractError, "attempts"):
            validate_probe_evidence(bad, device, run_id="a" * 32)

    def test_evidence_rejects_missing_read_and_forbidden_side_effect(self):
        spec = PROBE_SPECS["dwc3-0"]
        with self.assertRaisesRegex(ProbeContractError, "missing register read"):
            validate_probe_evidence(evidence("dwc3-0", spec.whitelist[:-1]), "dwc3-0", run_id="b" * 32)
        with self.assertRaisesRegex(ProbeContractError, "forbidden side effect"):
            text = evidence("dwc3-0", spec.whitelist)
            text = text.replace(
                "ASTERINAS_PROBE_RESOURCE",
                "Starting the controller\nASTERINAS_PROBE_RESOURCE",
                1,
            )
            validate_probe_evidence(text, "dwc3-0", run_id="c" * 32)

    def test_evidence_rejects_multiple_devices_and_bad_recovery(self):
        text = evidence("dwc3-0", PROBE_SPECS["dwc3-0"].whitelist)
        with self.assertRaisesRegex(ProbeContractError, "unexpected device marker"):
            validate_probe_evidence(text + "ASTERINAS_PROBE_BEGIN device=gmac-0\n", "dwc3-0", run_id="d" * 32)
        with self.assertRaisesRegex(ProbeContractError, "recovery epoch"):
            validate_probe_evidence(text.replace("U-Boot 2024.10\n", ""), "dwc3-0", run_id="e" * 32)

    def test_evidence_accepts_explicit_failed_probe_only_with_recovery(self):
        result = validate_probe_evidence(
            evidence(
                "gmac-1",
                (),
                status="fail",
                failure_reason="register-access",
            ),
            "gmac-1",
            run_id="f" * 32,
        )
        self.assertEqual(result.status, "fail")
        self.assertTrue(result.recovered)
        self.assertEqual(result.read_offsets, ())

    def test_timeout_evidence_requires_recovery_but_never_claims_pass(self):
        result = validate_probe_timeout_evidence(
            timeout_evidence("gmac-1"), "gmac-1", run_id="f" * 32
        )
        self.assertEqual(result.status, "timeout")
        self.assertTrue(result.recovered)
        self.assertEqual(result.read_offsets, ())
        with self.assertRaisesRegex(ProbeContractError, "completion marker"):
            validate_probe_timeout_evidence(
                evidence("gmac-1", PROBE_SPECS["gmac-1"].whitelist),
                "gmac-1",
                run_id="f" * 32,
            )

    def test_timeout_replay_classifies_pending_mmio_read(self):
        text = timeout_evidence("gmac-1", attempts=(0,))
        self.assertEqual(
            classify_probe_timeout(text, "gmac-1", run_id="f" * 32),
            "mmio-read:0x0000",
        )

    def test_failed_probe_cannot_claim_reads_or_pass(self):
        text = evidence(
            "gmac-1",
            (0x0110,),
            status="fail",
            failure_reason="register-access",
        )
        with self.assertRaisesRegex(ProbeContractError, "whitelist prefix"):
            validate_probe_evidence(text, "gmac-1", run_id="1" * 32)
        text = evidence("gmac-1", (), status="fail", failure_reason="register-access")
        text = text.replace("ASTERINAS_PROBE_FAIL device=gmac-1 reason=register-access", "ASTERINAS_PROBE_PASS device=gmac-1")
        with self.assertRaisesRegex(ProbeContractError, "failure reason"):
            validate_probe_evidence(text, "gmac-1", run_id="2" * 32)

    def test_readback_mismatch_retains_the_terminal_bad_read_as_failure(self):
        text = evidence(
            "gmac-1", (0,), status="fail", failure_reason="readback-mismatch"
        ).replace("value=0x00000000", "value=0xffffffff", 1)
        result = validate_probe_evidence(text, "gmac-1", run_id="6" * 32)
        self.assertEqual(result.status, "fail")
        self.assertTrue(result.recovered)
        self.assertEqual(result.read_offsets, (0,))
        for bad in (
            text.replace("reason=readback-mismatch", "reason=register-access"),
            evidence(
                "gmac-1", (0, 0xB0), status="fail", failure_reason="readback-mismatch"
            ).replace("value=0x00000000", "value=0xffffffff", 1),
        ):
            with self.subTest(transcript=bad):
                with self.assertRaises(ProbeContractError):
                    validate_probe_evidence(bad, "gmac-1", run_id="6" * 32)

    def test_resource_marker_must_be_inside_the_probe_interval(self):
        text = evidence("dwc3-0", PROBE_SPECS["dwc3-0"].whitelist)
        resource = next(line + "\n" for line in text.splitlines() if "PROBE_RESOURCE" in line)
        for bad in (resource + text.replace(resource, ""), text.replace(resource, "") + resource):
            with self.subTest(transcript=bad):
                with self.assertRaises(ProbeContractError):
                    validate_probe_evidence(bad, "dwc3-0", run_id="7" * 32)

    def test_dt_and_failure_markers_must_be_inside_probe_interval(self):
        text = evidence(
            "gmac-1", (0,), status="fail", failure_reason="readback-mismatch"
        ).replace("value=0x00000000", "value=0xffffffff", 1)
        dt = next(line + "\n" for line in text.splitlines() if "PROBE_DT" in line)
        failure = next(line + "\n" for line in text.splitlines() if "PROBE_FAIL" in line)
        for marker in (dt, failure):
            bad = marker + text.replace(marker, "")
            with self.subTest(marker=marker.strip()):
                with self.assertRaises(ProbeContractError):
                    validate_probe_evidence(bad, "gmac-1", run_id="8" * 32)

    def test_all_device_markers_belong_to_the_selected_probe(self):
        text = evidence("dwc3-0", PROBE_SPECS["dwc3-0"].whitelist)
        for marker in (
            "ASTERINAS_PROBE_RESOURCE device=dwc3-1 base=0x50490000 size=0x10000 irq=86",
            "ASTERINAS_PROBE_DT device=gmac-0 clocks=[3, 550] resets=[20, 7]",
        ):
            bad = text.replace(
                "ASTERINAS_PROBE_BEGIN device=dwc3-0",
                marker + "\nASTERINAS_PROBE_BEGIN device=dwc3-0",
                1,
            )
            with self.subTest(marker=marker):
                with self.assertRaisesRegex(ProbeContractError, "unexpected device marker"):
                    validate_probe_evidence(bad, "dwc3-0", run_id="9" * 32)

    def test_all_probe_markers_must_stay_inside_the_probe_interval(self):
        text = evidence("dwc3-0", PROBE_SPECS["dwc3-0"].whitelist)
        done = "ASTERINAS_PROBE_DONE device=dwc3-0 status=pass\n"
        bad = done + text
        with self.assertRaisesRegex(ProbeContractError, "outside probe interval"):
            validate_probe_evidence(bad, "dwc3-0", run_id="0" * 32)

    def test_gmac_requires_both_recovered_usb_probes(self):
        validate_prerequisites("gmac-0", {"dwc3-0", "dwc3-1"})
        with self.assertRaisesRegex(ProbeContractError, "missing prerequisite"):
            validate_prerequisites("gmac-1", {"dwc3-0"})

    def test_kernel_probe_paths_are_read_only_and_guard_normal_initializers(self):
        root = Path(__file__).parents[3]
        usb = (root / "kernel/comps/usb/src/arch/riscv/mod.rs").read_text()
        dwmac = (root / "kernel/comps/dwmac/src/arch/riscv.rs").read_text()
        probe_start = usb.index("fn probe_read_only")
        usb_probe = usb[probe_start : usb.index("#[derive(Clone, Copy, Debug, Eq, PartialEq)]", probe_start)]
        probe_start = dwmac.index("pub(super) fn probe_read_only")
        gmac_probe = dwmac[probe_start : dwmac.index("#[cfg(ktest)]", probe_start)]
        for source in (usb_probe, gmac_probe):
            self.assertNotRegex(source, r"\b(?:write_once|write_bytes|write)\s*(?:<|\()")
            self.assertNotRegex(source, r"\b(?:dma|mdio|phy)\b")
        self.assertNotIn("program_platform", gmac_probe)
        self.assertLess(usb.index("if aster_cmdline::HARDWARE_PROBE.get().is_some()"), usb.index("pci::init()"))


if __name__ == "__main__":
    unittest.main()
