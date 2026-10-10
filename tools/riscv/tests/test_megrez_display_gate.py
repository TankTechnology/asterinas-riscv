# SPDX-License-Identifier: MPL-2.0

import json
from pathlib import Path
import tempfile
import unittest

from tools.riscv import megrez_display_gate as gate


class DisplayGateTests(unittest.TestCase):
    def test_qemu_receipt_requires_desktop_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps({"passed": False, "profile": "desktop-m4"}))
            with self.assertRaises(gate.DisplayGateError):
                gate.validate_qemu_receipt(path)

    def test_pattern_only_receipt_cannot_authorize_physical_desktop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps({
                "passed": True, "profile": "desktop-m4", "input_sha256": {},
                "qualification": "fbdev-pattern-only",
                "application_paint_verified": False,
            }))
            with self.assertRaises(gate.DisplayGateError):
                gate.validate_qemu_receipt(path)

    def test_pattern_only_receipt_requires_explicit_physical_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps({
                "passed": True, "profile": "desktop-m4", "input_sha256": {},
                "qualification": "fbdev-pattern-only",
                "application_paint_verified": False,
                "screenshot": {"distinct_sampled_colors": 3, "non_background_pixels": 10},
            }))
            receipt = gate.validate_qemu_receipt(path, allow_pattern_only=True)
            self.assertFalse(receipt["application_paint_verified"])

    def test_candidate_identity_matches_shared_kernel_and_stage1(self):
        receipt = {"input_sha256": {"kernel": "k", "stage1_initramfs": "s"}}
        document = {
            "artifacts": {
                "kernel": {"sha256": "k"},
                "desktop_stage1": {"sha256": "s"},
            }
        }
        gate.validate_candidate_identity(receipt, document)

    def test_candidate_identity_rejects_stale_kernel(self):
        receipt = {"input_sha256": {"kernel": "old", "stage1_initramfs": "s"}}
        document = {
            "artifacts": {
                "kernel": {"sha256": "new"},
                "desktop_stage1": {"sha256": "s"},
            }
        }
        with self.assertRaisesRegex(gate.DisplayGateError, "kernel differs"):
            gate.validate_candidate_identity(receipt, document)

    def test_candidate_identity_checks_the_initramfs_selected_by_desktop(self):
        receipt = {"input_sha256": {"kernel": "k", "stage1_initramfs": "basic"}}
        document = {"artifacts": {
            "kernel": {"sha256": "k"},
            "stage1": {"sha256": "basic"},
            "desktop_stage1": {"sha256": "different-desktop"},
        }}
        with self.assertRaisesRegex(gate.DisplayGateError, "stage1_initramfs differs"):
            gate.validate_candidate_identity(receipt, document)

    def test_install_receipt_requires_framebuffer_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "install.json"
            path.write_text(json.dumps({
                "passed": True, "service": "asterinas-single-packet", "rootfs_sha256": "a" * 64,
            }))
            with self.assertRaisesRegex(gate.DisplayGateError, "framebuffer helper"):
                gate.validate_install_receipt(path)

    def test_install_receipt_accepts_framebuffer_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "install.json"
            path.write_text(json.dumps({
                "passed": True, "service": "desktop-m4-framebuffer-rw", "rootfs_sha256": "a" * 64,
            }))
            self.assertEqual(
                gate.validate_install_receipt(path)["service"],
                "desktop-m4-framebuffer-rw",
            )

    def test_installed_rootfs_must_be_the_qemu_qualified_image(self):
        with self.assertRaisesRegex(gate.DisplayGateError, "rootfs differs"):
            gate.validate_installed_identity(
                {"rootfs_sha256": "a" * 64},
                {"input_sha256": {"root_image": "b" * 64}},
            )

    def test_installed_rootfs_missing_qemu_identity_is_rejected(self):
        with self.assertRaises(gate.DisplayGateError):
            gate.validate_installed_identity({"rootfs_sha256": "a" * 64}, {})

    def test_installed_rootfs_matches_qemu_image(self):
        gate.validate_installed_identity(
            {"rootfs_sha256": "a" * 64},
            {"input_sha256": {"root_image": "a" * 64}},
        )

    def test_selector_rejects_hardware_and_network_tokens(self):
        document = {
            "schema_version": 3,
            "vendor": "default rockos\nlabel rockos\nlinux /x\ninitrd /y\nfdt /z\nappend quiet\n",
            "artifacts": {},
            "desktop_args": "console=ttyS0 -- --root-init=systemd",
            "extlinux": "label asterinas-desktop\nappend asterinas.net=eic7700-rj45",
        }
        with self.assertRaises(gate.DisplayGateError):
            gate.validate_display_manifest(document)

    def test_framebuffer_restore_hash_must_match(self):
        transcript = (
            "DEBIAN_DESKTOP_M4_FB_RW width=1280 height=1024 stride=5120 "
            "bpp=32 format=BgrReserved rect=16x16 status=pass "
            "saved_sha256=" + "a" * 64 + " restored_sha256=" + "b" * 64 + "\n"
        )
        with self.assertRaises(gate.DisplayGateError):
            gate.validate_framebuffer_receipt(transcript)

    def test_framebuffer_marker_is_machine_checkable(self):
        digest = "a" * 64
        transcript = (
            "DEBIAN_DESKTOP_M4_FB_RW width=1280 height=1024 stride=5120 "
            "bpp=32 format=BgrReserved rect=16x16 status=pass "
            f"saved_sha256={digest} restored_sha256={digest}\r\n"
        )
        result = gate.validate_framebuffer_receipt(transcript)
        self.assertEqual(result["width"], "1280")
        self.assertEqual(result["stride"], "5120")

    def test_recovery_requires_root_and_nonce_bound_boot_id(self):
        transcript = (
            "ASTERINAS_DISPLAY_RECOVERY nonce=" + "a" * 32
            + " uid=1000 boot_id=11111111-2222-3333-4444-555555555555\n"
        )
        with self.assertRaisesRegex(gate.DisplayGateError, "UID 0"):
            gate.validate_recovery_marker(transcript, "a" * 32)

    def test_recovery_accepts_exact_root_marker(self):
        nonce = "b" * 32
        transcript = (
            f"ASTERINAS_DISPLAY_RECOVERY nonce={nonce} "
            "uid=0 boot_id=11111111-2222-3333-4444-555555555555\r\n"
        )
        result = gate.validate_recovery_marker(transcript, nonce)
        self.assertEqual(result["uid"], 0)
        self.assertEqual(result["boot_id"], "11111111-2222-3333-4444-555555555555")


if __name__ == "__main__":
    unittest.main()
