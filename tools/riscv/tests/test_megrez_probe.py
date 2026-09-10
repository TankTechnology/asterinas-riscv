#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Tests for the bounded Megrez kernel-probe loop."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest
import zlib

from tools.riscv import megrez_probe as probe
from tools.riscv.megrez_debug_contract import ArtifactIdentity, DebugPlan


SERIAL_DEVICE = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_TEST-if00-port0"
MMC_PATHS = {
    "kernel": "asterinas-test.Image",
    "initramfs": "asterinas-test-stage1.cpio",
    "megrez_dtb": "dtbs/linux/eswin/eic7700-milkv-megrez.dtb",
}


def _plan() -> DebugPlan:
    addresses = {
        "kernel": 0x80200000,
        "initramfs": 0x83000000,
        "qemu_dtb": 0xF0000000,
        "megrez_dtb": 0xF0000000,
    }
    artifacts = tuple(
        ArtifactIdentity(
            name=name,
            path=str((Path("/tmp") / name).absolute()),
            load_address=addresses[name],
            size=100 + index,
            sha256=hashlib.sha256(name.encode()).hexdigest(),
            crc32=f"{zlib.crc32(name.encode()):08x}",
        )
        for index, name in enumerate(("kernel", "initramfs", "qemu_dtb", "megrez_dtb"))
    )
    return DebugPlan(
        schema_version=1,
        profile="tcp-probe",
        artifacts=artifacts,
        bootargs="loglevel=info init=/init asterinas.reboot_after=180",
        smp=4,
        sv39=True,
        markers=("Enter riscv_boot", "ASTERINAS_GMAC_TCP_PROBE_READY"),
        reboot_after=180,
    )


def _bundle_mapping() -> dict[str, object]:
    plan = _plan()
    return {
        "schema_version": 1,
        "plan": plan.to_dict(),
        "plan_sha256": plan.plan_sha256,
        "device": SERIAL_DEVICE,
        "mmc_artifacts": [
            {"name": name, "path": MMC_PATHS[name]}
            for name in ("kernel", "initramfs", "megrez_dtb")
        ],
    }


def _encoded(mapping: dict[str, object]) -> bytes:
    return (json.dumps(mapping, sort_keys=True, separators=(",", ":")) + "\n").encode()


class ProbeBundleTests(unittest.TestCase):
    def test_canonical_bundle_round_trip_is_hash_bound(self) -> None:
        bundle = probe.ProbeBundle.from_bytes(_encoded(_bundle_mapping()))

        self.assertEqual(bundle.device, SERIAL_DEVICE)
        self.assertEqual(bundle.plan.plan_sha256, bundle.plan_sha256)
        self.assertEqual(
            tuple(item.name for item in bundle.mmc_artifacts),
            ("kernel", "initramfs", "megrez_dtb"),
        )
        self.assertEqual(
            bundle.bundle_sha256,
            hashlib.sha256(bundle.canonical_bytes()).hexdigest(),
        )
        self.assertEqual(probe.ProbeBundle.from_bytes(bundle.canonical_bytes()), bundle)

    def test_bundle_rejects_unknown_fields(self) -> None:
        mapping = _bundle_mapping()
        mapping["extra"] = "unsafe"

        with self.assertRaisesRegex(probe.ProbeContractError, "fields"):
            probe.ProbeBundle.from_bytes(_encoded(mapping))

    def test_bundle_rejects_non_by_id_serial_device(self) -> None:
        mapping = _bundle_mapping()
        mapping["device"] = "/dev/ttyUSB0"

        with self.assertRaisesRegex(probe.ProbeContractError, "serial device"):
            probe.ProbeBundle.from_bytes(_encoded(mapping))

    def test_bundle_rejects_unsafe_mmc_paths(self) -> None:
        for invalid in ("/boot/kernel", "../kernel", "dir//kernel", "dir/./kernel"):
            with self.subTest(invalid=invalid):
                mapping = _bundle_mapping()
                mapping["mmc_artifacts"][0]["path"] = invalid  # type: ignore[index]

                with self.assertRaisesRegex(probe.ProbeContractError, "MMC path"):
                    probe.ProbeBundle.from_bytes(_encoded(mapping))

    def test_bundle_rejects_mmc_artifact_name_mismatch(self) -> None:
        mapping = _bundle_mapping()
        mapping["mmc_artifacts"][0]["name"] = "initramfs"  # type: ignore[index]

        with self.assertRaisesRegex(probe.ProbeContractError, "canonical order"):
            probe.ProbeBundle.from_bytes(_encoded(mapping))

    def test_bundle_rejects_changed_plan_identity_fields(self) -> None:
        for field, value in (
            ("size", 999),
            ("crc32", "f" * 8),
            ("load_address", 0x80400000),
        ):
            with self.subTest(field=field):
                mapping = _bundle_mapping()
                mapping["plan"]["artifacts"][0][field] = value  # type: ignore[index]

                with self.assertRaises(probe.ProbeContractError):
                    probe.ProbeBundle.from_bytes(_encoded(mapping))

    def test_bundle_rejects_plan_digest_mismatch(self) -> None:
        mapping = _bundle_mapping()
        mapping["plan_sha256"] = "f" * 64

        with self.assertRaisesRegex(probe.ProbeContractError, "plan digest"):
            probe.ProbeBundle.from_bytes(_encoded(mapping))


class ProbeSelectionTests(unittest.TestCase):
    def test_probe_names_preserve_requested_order(self) -> None:
        selected = probe.validate_probe_names(("syscall272", "boot", "syscall213"))

        self.assertEqual(
            tuple(definition.name for definition in selected),
            ("syscall272", "boot", "syscall213"),
        )

    def test_all_fixed_probe_names_are_accepted(self) -> None:
        names = (
            "boot",
            "syscall213",
            "syscall272",
            "ext2-writeback",
            "systemd-compat",
        )

        self.assertEqual(
            tuple(item.name for item in probe.validate_probe_names(names)), names
        )

    def test_invalid_probe_selections_are_rejected(self) -> None:
        for names in (
            (),
            ("boot", "boot"),
            ("unknown",),
            (" boot",),
            (
                "boot",
                "syscall213",
                "syscall272",
                "ext2-writeback",
                "systemd-compat",
                "x",
            ),
        ):
            with self.subTest(names=names), self.assertRaises(probe.ProbeContractError):
                probe.validate_probe_names(names)

    def test_session_seconds_are_bounded_canonical_integers(self) -> None:
        self.assertEqual(probe.validate_session_seconds(None), 90)
        self.assertEqual(probe.validate_session_seconds(30), 30)
        self.assertEqual(probe.validate_session_seconds(300), 300)
        for invalid in (True, 29, 301, 30.0):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(probe.ProbeContractError),
            ):
                probe.validate_session_seconds(invalid)


class ProbeProtocolTests(unittest.TestCase):
    NONCE = "00112233445566778899aabbccddeeff"

    def _success(self) -> bytes:
        return (
            "kernel boot noise\n"
            "ASTERINAS_PROBE_READY v=1 pid=1\n"
            f"ASTERINAS_PROBE_START v=1 nonce={self.NONCE} seq=0 name=boot\n"
            f"ASTERINAS_PROBE_PASS v=1 nonce={self.NONCE} seq=0 name=boot detail=boot-ok\n"
            f"ASTERINAS_PROBE_START v=1 nonce={self.NONCE} seq=1 name=syscall213\n"
            f"ASTERINAS_PROBE_PASS v=1 nonce={self.NONCE} seq=1 name=syscall213 detail=enosys\n"
            f"ASTERINAS_PROBE_DONE v=1 nonce={self.NONCE} count=2 status=pass\n"
            f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={self.NONCE}\n"
        ).encode()

    def test_classifies_one_complete_ordered_exchange(self) -> None:
        exchange = probe.classify_probe_transcript(
            self._success(), self.NONCE, ("boot", "syscall213")
        )

        self.assertTrue(exchange.passed)
        self.assertEqual(
            tuple(
                (item.sequence, item.name, item.passed, item.detail)
                for item in exchange.outcomes
            ),
            ((0, "boot", True, "boot-ok"), (1, "syscall213", True, "enosys")),
        )
        self.assertEqual(exchange.dmesg, b"")

    def test_classifies_fail_fast_exchange_with_bounded_dmesg(self) -> None:
        transcript = (
            "ASTERINAS_PROBE_READY v=1 pid=1\n"
            f"ASTERINAS_PROBE_START v=1 nonce={self.NONCE} seq=0 name=boot\n"
            f"ASTERINAS_PROBE_FAIL v=1 nonce={self.NONCE} seq=0 name=boot errno=5 detail=uname-failed\n"
            f"ASTERINAS_PROBE_DMESG_BEGIN v=1 nonce={self.NONCE} bytes=12\n"
            "hello dmesg\n"
            f"ASTERINAS_PROBE_DMESG_END v=1 nonce={self.NONCE}\n"
            f"ASTERINAS_PROBE_DONE v=1 nonce={self.NONCE} count=1 status=fail\n"
            f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={self.NONCE}\n"
        ).encode()

        exchange = probe.classify_probe_transcript(
            transcript, self.NONCE, ("boot", "syscall213")
        )

        self.assertFalse(exchange.passed)
        self.assertEqual(exchange.outcomes[0].error_number, 5)
        self.assertEqual(exchange.dmesg, b"hello dmesg\n")

    def test_request_encoding_is_exact_and_shell_is_preselected(self) -> None:
        selected = probe.validate_probe_names(("boot", "syscall213"))

        self.assertEqual(
            probe.encode_probe_request(self.NONCE, selected),
            (
                f"ASTERINAS_PROBE_RUN v=1 nonce={self.NONCE} "
                "probes=boot,syscall213 shell=0\n"
            ).encode(),
        )
        self.assertEqual(
            probe.encode_probe_request(self.NONCE, selected, shell=True),
            (
                f"ASTERINAS_PROBE_RUN v=1 nonce={self.NONCE} "
                "probes=boot,syscall213 shell=1\n"
            ).encode(),
        )

    def test_protocol_rejects_missing_or_replayed_terminal_records(self) -> None:
        valid = self._success().decode()
        variants = (
            valid.replace(
                f"ASTERINAS_PROBE_DONE v=1 nonce={self.NONCE} count=2 status=pass\n",
                "",
            ),
            valid.replace(
                f"ASTERINAS_PROBE_PASS v=1 nonce={self.NONCE} seq=0 name=boot detail=boot-ok\n",
                f"ASTERINAS_PROBE_PASS v=1 nonce={self.NONCE} seq=0 name=boot detail=boot-ok\n"
                f"ASTERINAS_PROBE_PASS v=1 nonce={self.NONCE} seq=0 name=boot detail=boot-ok\n",
            ),
            valid.replace("seq=1 name=syscall213", "seq=0 name=syscall213"),
        )
        for transcript in variants:
            with (
                self.subTest(transcript=transcript[-200:]),
                self.assertRaises(probe.ProbeProtocolError),
            ):
                probe.classify_probe_transcript(
                    transcript.encode(), self.NONCE, ("boot", "syscall213")
                )

    def test_protocol_rejects_identity_substitution_and_unknown_markers(self) -> None:
        valid = self._success().decode()
        variants = (
            valid.replace(self.NONCE, "f" * 32, 1),
            valid.replace("seq=1 name=syscall213", "seq=1 name=syscall272", 1),
            valid.replace("detail=enosys", "detail=unsafe/value"),
            valid.replace(
                f"ASTERINAS_PROBE_DONE v=1 nonce={self.NONCE}",
                f"ASTERINAS_PROBE_UNKNOWN v=1 nonce={self.NONCE}\n"
                f"ASTERINAS_PROBE_DONE v=1 nonce={self.NONCE}",
            ),
            valid.replace("count=2 status=pass", "count=1 status=pass"),
        )
        for transcript in variants:
            with (
                self.subTest(transcript=transcript[-200:]),
                self.assertRaises(probe.ProbeProtocolError),
            ):
                probe.classify_probe_transcript(
                    transcript.encode(), self.NONCE, ("boot", "syscall213")
                )

    def test_protocol_rejects_pass_after_failure(self) -> None:
        transcript = (
            "ASTERINAS_PROBE_READY v=1 pid=1\n"
            f"ASTERINAS_PROBE_START v=1 nonce={self.NONCE} seq=0 name=boot\n"
            f"ASTERINAS_PROBE_FAIL v=1 nonce={self.NONCE} seq=0 name=boot errno=5 detail=uname-failed\n"
            f"ASTERINAS_PROBE_START v=1 nonce={self.NONCE} seq=1 name=syscall213\n"
            f"ASTERINAS_PROBE_PASS v=1 nonce={self.NONCE} seq=1 name=syscall213 detail=enosys\n"
            f"ASTERINAS_PROBE_DONE v=1 nonce={self.NONCE} count=2 status=fail\n"
            f"ASTERINAS_PROBE_REBOOT_READY v=1 nonce={self.NONCE}\n"
        ).encode()

        with self.assertRaises(probe.ProbeProtocolError):
            probe.classify_probe_transcript(
                transcript, self.NONCE, ("boot", "syscall213")
            )


if __name__ == "__main__":
    unittest.main()
