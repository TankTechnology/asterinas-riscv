#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from qemu_uboot_matrix import (  # noqa: E402
    ArtifactBundle,
    MATRIX_LEGS,
    _parse_args,
    _selected_legs,
    build_leg_receipt,
    classify_qemu_device_help,
    classify_serial_observations,
    diagnose_leg,
    probe_qemu_capabilities,
    run_matrix,
)


class QemuUbootMatrixTests(unittest.TestCase):
    def test_leg_receipt_keeps_identity_recovery_and_tail(self) -> None:
        boot_id = "01234567-89ab-cdef-0123-456789abcdef"
        receipt = build_leg_receipt(
            result_payload={
                "session": {
                    "recovery_complete": True,
                    "recovery_elapsed_seconds": 1.25,
                }
            },
            serial_text=(
                f"OpenSBI v1.7 boot_id={boot_id}\n"
                "U-Boot 2026.07\n"
                "ASTERINAS_REBOOT_TIMER_READY\n"
                f"OpenSBI v1.7 boot_id={boot_id}\n"
                "U-Boot 2026.07\n=> \n"
            ),
            returncode=0,
            status="PASS",
            passed=True,
            failure=None,
            diagnosis=None,
        )
        self.assertEqual(receipt["schema"], "asterinas.qemu-uboot-leg-receipt.v1")
        self.assertEqual(receipt["execution_scope"], "qemu-guest")
        self.assertEqual(receipt["hardware_state"], "not-accessed")
        self.assertEqual(receipt["recovery_state"], "recovered")
        self.assertEqual(receipt["fallback_state"], "uboot-prompt")
        self.assertEqual(receipt["watchdog_state"], "armed")
        self.assertEqual(receipt["watchdog_kind"], "software-recovery-timer")
        self.assertEqual(receipt["boot_ids"], [boot_id])
        self.assertEqual(receipt["opensbi_epochs"], 2)
        self.assertEqual(receipt["uboot_epochs"], 2)
        self.assertIn("=> ", receipt["serial_tail"])
        self.assertEqual(len(receipt["serial_tail_sha256"]), 64)

    def test_leg_receipt_records_guest_boot_id_state(self) -> None:
        boot_id = "01234567-89ab-cdef-0123-456789abcdef"
        receipt = build_leg_receipt(
            result_payload={"session": {"recovery_complete": True}},
            serial_text=(
                f"ASTERINAS_BOOT_ID boot_id={boot_id}\n"
                "ASTERINAS_REBOOT_TIMER_READY\n"
            ),
            returncode=0,
            status="PASS",
            passed=True,
            failure=None,
            diagnosis=None,
        )
        self.assertEqual(receipt["boot_id_state"], "observed")
        self.assertEqual(receipt["guest_boot_id"], boot_id)

    def test_leg_receipt_exposes_missing_guest_boot_id(self) -> None:
        receipt = build_leg_receipt(
            result_payload={"session": {"recovery_complete": True}},
            serial_text="ASTERINAS_REBOOT_TIMER_READY\n",
            returncode=0,
            status="PASS",
            passed=True,
            failure=None,
            diagnosis=None,
        )
        self.assertEqual(receipt["boot_id_state"], "missing")
        self.assertIsNone(receipt["guest_boot_id"])
        self.assertEqual(receipt["watchdog_state"], "armed")
        self.assertEqual(receipt["watchdog_kind"], "software-recovery-timer")

    def test_leg_receipt_marks_missing_recovery_as_not_recovered(self) -> None:
        receipt = build_leg_receipt(
            result_payload={
                "session": {
                    "recovery_complete": False,
                    "recovery_elapsed_seconds": None,
                }
            },
            serial_text="OpenSBI v1.7\nASTERINAS_REBOOT_TIMER_READY\n",
            returncode=1,
            status="FAIL",
            passed=False,
            failure="recovery-timeout:opensbi",
            diagnosis="recovery-epoch-missing:opensbi",
        )
        self.assertEqual(receipt["recovery_state"], "not-recovered")
        self.assertEqual(receipt["failure"], "recovery-timeout:opensbi")
        self.assertEqual(receipt["diagnosis"], "recovery-epoch-missing:opensbi")

    def test_leg_receipt_distinguishes_rockos_login_from_uboot_prompt(self) -> None:
        receipt = build_leg_receipt(
            result_payload={"session": {"recovery_complete": True}},
            serial_text=(
                "OpenSBI v1.7\nU-Boot 2026.07\n=> \n"
                "ASTERINAS_REBOOT_TIMER_READY\n"
                "OpenSBI v1.7\nU-Boot 2026.07\n"
                "rockos-eswin login:\n"
            ),
            returncode=0,
            status="PASS",
            passed=True,
            failure=None,
            diagnosis=None,
        )
        self.assertEqual(receipt["fallback_state"], "rockos-login")

        early_login = build_leg_receipt(
            result_payload={"session": {"recovery_complete": True}},
            serial_text="rockos-eswin login:\nASTERINAS_REBOOT_TIMER_READY\n",
            returncode=0,
            status="PASS",
            passed=True,
            failure=None,
            diagnosis=None,
        )
        self.assertEqual(early_login["fallback_state"], "unknown")

        uboot_only = build_leg_receipt(
            result_payload={"session": {"recovery_complete": True}},
            serial_text=(
                "OpenSBI v1.7\nU-Boot 2026.07\n=> \n"
                "ASTERINAS_REBOOT_TIMER_READY\n"
                "OpenSBI v1.7\nU-Boot 2026.07\n=> \n"
            ),
            returncode=0,
            status="PASS",
            passed=True,
            failure=None,
            diagnosis=None,
        )
        self.assertEqual(uboot_only["fallback_state"], "uboot-prompt")

    def test_leg_receipt_does_not_promote_rockos_diagnostic_text_to_login(self) -> None:
        receipt = build_leg_receipt(
            result_payload={"session": {"recovery_complete": True}},
            serial_text=(
                "OpenSBI v1.7\n"
                "RockOS login marker was not observed: rockos-eswin login:\n"
                "=> \n"
            ),
            returncode=0,
            status="PASS",
            passed=True,
            failure=None,
            diagnosis=None,
        )
        self.assertEqual(receipt["fallback_state"], "unknown")

    def test_initial_uboot_prompt_is_not_recovery(self) -> None:
        receipt = build_leg_receipt(
            result_payload={"session": {"recovery_complete": False}},
            serial_text="OpenSBI v1.7\nU-Boot 2026.07\n=> \n",
            returncode=1,
            status="FAIL",
            passed=False,
            failure="boot-timeout",
            diagnosis="boot-timeout-before-kernel",
        )
        self.assertEqual(receipt["fallback_state"], "unknown")

    def test_leg_receipt_does_not_call_a_complete_nonrecovery_boot_failed(self) -> None:
        receipt = build_leg_receipt(
            result_payload={
                "validation_scenario": None,
                "session": {"recovery_complete": False},
            },
            serial_text="OpenSBI v1.7\nU-Boot 2026.07\n=> \n",
            returncode=0,
            status="PASS",
            passed=True,
            failure=None,
            diagnosis=None,
        )
        self.assertEqual(receipt["recovery_state"], "not-requested")

    def _bundles(self, root: Path) -> dict[str, ArtifactBundle]:
        return {
            name: ArtifactBundle(
                name,
                root / f"{name}-u-boot",
                root / f"{name}-boot.ext4",
                root / f"{name}-artifacts.json",
                root / f"{name}-dtb-audit.json",
            )
            for name in ("qemu-virt", "sifive-u")
        }

    def test_selected_legs_are_closed_and_duplicates_rejected(self) -> None:
        self.assertEqual(_selected_legs("qemu-virt-headless"), (MATRIX_LEGS[0],))
        with self.assertRaisesRegex(ValueError, "unknown"):
            _selected_legs("unknown")
        with self.assertRaisesRegex(ValueError, "must not repeat"):
            _selected_legs("qemu-virt-headless,qemu-virt-headless")

    def test_qemu_device_help_classifies_gmac_without_treating_virtio_as_gmac(
        self,
    ) -> None:
        report = classify_qemu_device_help(
            """Network devices:
name "virtio-net-device", bus virtio-bus
name "dwmac-eic7700", bus sysbus
name "e1000", bus PCI
Storage devices:
name "virtio-blk-device", bus virtio-bus
"""
        )
        self.assertEqual(
            report,
            {
                "network_models": ["dwmac-eic7700", "e1000", "virtio-net-device"],
                "gmac_models": ["dwmac-eic7700"],
                "gmac_model_present": True,
            },
        )

    def test_qemu_capability_probe_keeps_raw_inventory_and_version(self) -> None:
        def fake_run(command: list[str], **_kwargs: object):
            if command[-1] == "--version":
                return mock.Mock(returncode=0, stdout="QEMU 10.2.1\n", stderr="")
            return mock.Mock(
                returncode=0,
                stdout='Network devices:\nname "virtio-net-device"\n',
                stderr="",
            )

        report, raw = probe_qemu_capabilities(run_command=fake_run)
        self.assertEqual(report["version"], "QEMU 10.2.1")
        self.assertFalse(report["gmac_model_present"])
        self.assertIn("virtio-net-device", raw)

    def test_failure_diagnosis_separates_boot_and_recovery_boundaries(self) -> None:
        self.assertEqual(
            diagnose_leg(
                failure="boot-timeout",
                serial_text="Starting kernel ...\n>>> Hello from RISC-V userspace on Asterinas! <<<\n",
                runner_stderr="",
            ),
            "recovery-marker-missing-after-kernel",
        )
        self.assertEqual(
            diagnose_leg(
                failure="boot-timeout",
                serial_text="U-Boot 2026.07\n",
                runner_stderr="",
            ),
            "boot-timeout-before-kernel",
        )
        self.assertEqual(
            diagnose_leg(
                failure="recovery-timeout:opensbi",
                serial_text="ASTERINAS_REBOOT_TIMER_READY\n",
                runner_stderr="",
            ),
            "recovery-epoch-missing:opensbi",
        )

    def test_serial_observations_identify_device_paths_without_guessing_gmac(
        self,
    ) -> None:
        xhci = classify_serial_observations(
            MATRIX_LEGS[2],
            "usb: PCI xHCI selected: 0000:00:01.0 irq=3:33\n"
            "usb: Starting PCI xHCI host: 0000:00:01.0\n"
            "ASTERINAS_REBOOT_TIMER_READY\n",
        )
        self.assertEqual(
            xhci,
            ("xhci-host-started", "xhci-kernel-selected", "timer-recovery-marker"),
        )
        gem = classify_serial_observations(
            MATRIX_LEGS[4],
            "DHCP client bound to address 10.0.2.15\nTFTP from server 10.0.2.2\n",
        )
        self.assertEqual(gem, ("gem-uboot-network-active",))
        minimal = classify_serial_observations(
            MATRIX_LEGS[3], "ASTERINAS_REBOOT_TIMER_READY\n"
        )
        self.assertEqual(
            minimal,
            ("minimal-network-silent", "usb-disabled", "timer-recovery-marker"),
        )

    def test_parse_defaults_to_all_registered_legs_and_one_attempt(self) -> None:
        args = _parse_args(
            ["run", "--output", "/tmp/matrix"]
            + [
                item
                for prefix in ("qemu-virt", "sifive-u")
                for item in (
                    f"--{prefix}-uboot",
                    f"/tmp/{prefix}-u-boot",
                    f"--{prefix}-boot-disk",
                    f"/tmp/{prefix}.ext4",
                    f"--{prefix}-manifest",
                    f"/tmp/{prefix}.json",
                    f"--{prefix}-dtb-audit",
                    f"/tmp/{prefix}-dtb.json",
                )
            ]
        )
        self.assertEqual(args.repeat, 1)
        self.assertIsNone(args.require_fallback)
        self.assertEqual(args.legs, ",".join(leg.name for leg in MATRIX_LEGS))

    def test_matrix_can_require_real_rockos_fallback(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundles = self._bundles(root)

            def fake_run(command: list[str], **_kwargs: object):
                result = Path(command[command.index("--result") + 1])
                result.parent.mkdir(parents=True, exist_ok=True)
                result.write_text(
                    json.dumps(
                        {
                            "passed": True,
                            "status": "PASS",
                            "session": {"recovery_complete": True},
                        }
                    )
                    + "\n"
                )
                serial = result.parent / "serial.log"
                serial.write_text(
                    "OpenSBI v1.7\nU-Boot 2026.07\n=> \n"
                    "ASTERINAS_REBOOT_TIMER_READY\n"
                    "OpenSBI v1.7\nU-Boot 2026.07\n=> \n",
                    encoding="utf-8",
                )
                return mock.Mock(returncode=0, stdout="", stderr="")

            summary, exit_code = run_matrix(
                output=root / "matrix",
                bundles=bundles,
                legs=(MATRIX_LEGS[0],),
                run_command=fake_run,
                required_fallback="rockos-login",
            )
            self.assertEqual(exit_code, 1)
            self.assertFalse(summary["passed"])
            self.assertEqual(summary["required_fallback"], "rockos-login")
            leg = summary["legs"][0]
            self.assertEqual(leg["failure"], "fallback-state-mismatch")
            self.assertEqual(leg["diagnosis"], "fallback-state-required:rockos-login")

    def test_matrix_continues_after_failure_and_writes_summary(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls: list[list[str]] = []

            def fake_run(command: list[str], **_kwargs: object):
                calls.append(command)
                result = Path(command[command.index("--result") + 1])
                result.parent.mkdir(parents=True, exist_ok=True)
                passed = len(calls) != 1
                result.write_text(
                    json.dumps(
                        {
                            "passed": passed,
                            "status": "PASS" if passed else "FAIL",
                            "session": {"failure": None if passed else "boot-timeout"},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return mock.Mock(
                    returncode=0 if passed else 1,
                    stdout="runner output\n",
                    stderr="runner error\n" if not passed else "",
                )

            selected = MATRIX_LEGS[:2]
            summary, exit_code = run_matrix(
                output=root / "matrix",
                bundles=self._bundles(root),
                legs=selected,
                run_command=fake_run,
            )
            self.assertEqual(exit_code, 1)
            self.assertEqual(len(calls), 2)
            self.assertFalse(summary["passed"])
            self.assertEqual(summary["schema"], "asterinas.qemu-uboot-matrix.v6")
            self.assertEqual(
                summary["qemu_capabilities"],
                {"available": False, "reason": "not-probed"},
            )
            self.assertEqual(
                json.loads((root / "matrix" / "matrix.json").read_text())["legs"][1][
                    "status"
                ],
                "PASS",
            )
            self.assertIsNone(summary["legs"][1]["failure"])
            self.assertEqual(
                summary["legs"][0]["receipt"]["recovery_state"],
                "not-requested",
            )
            self.assertEqual(
                summary["legs"][0]["diagnosis"], "boot-timeout-before-kernel"
            )
            self.assertEqual(
                (
                    root / "matrix" / summary["legs"][1]["runner_stdout_path"]
                ).read_text(),
                "runner output\n",
            )
            self.assertEqual(
                (
                    root / "matrix" / summary["legs"][0]["runner_stderr_path"]
                ).read_text(),
                "runner error\n",
            )

    def test_runner_error_is_classified_without_stopping_later_legs(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = 0

            def unavailable(_command: list[str], **_kwargs: object):
                nonlocal calls
                calls += 1
                raise OSError("qemu missing")

            summary, exit_code = run_matrix(
                output=root / "matrix",
                bundles=self._bundles(root),
                legs=MATRIX_LEGS[:2],
                run_command=unavailable,
            )
            self.assertEqual(exit_code, 1)
            self.assertEqual(calls, 2)
            self.assertEqual(
                [entry["failure"] for entry in summary["legs"]],
                ["runner-exit:127", "runner-exit:127"],
            )

    def test_existing_nonempty_output_is_never_overwritten(self) -> None:
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "matrix"
            output.mkdir()
            (output / "sentinel").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "new or empty"):
                run_matrix(
                    output=output,
                    bundles=self._bundles(Path(temporary)),
                    legs=MATRIX_LEGS[:1],
                )

    def test_output_file_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "matrix"
            output.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "new or empty"):
                run_matrix(
                    output=output,
                    bundles=self._bundles(Path(temporary)),
                    legs=MATRIX_LEGS[:1],
                )


if __name__ == "__main__":
    unittest.main()
