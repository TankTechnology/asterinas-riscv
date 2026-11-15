# SPDX-License-Identifier: MPL-2.0

import importlib.util
from pathlib import Path
import unittest


SOURCE = Path(__file__).parents[1] / "diagnostics/klog_micro_gate.py"
FOLLOW_PROBE = Path(__file__).parents[1] / "diagnostics/klog_dmesg_probe.c"
SPEC = importlib.util.spec_from_file_location("klog_micro_gate", SOURCE)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def transcript():
    return "\n".join(
        [
            "KLOG_MICRO_BEGIN",
            *(
                f"test_{case} summary: 1 tests passed, 0 tests failed"
                for case in gate.REQUIRED_CASES
            ),
            "KLOG_CAPTURE_INFO_RETAINED=1",
            "KLOG_MICRO_PROBE_EXIT=0",
            "KLOG_CONSOLE_BEGIN",
            "KLOG_CONSOLE_PHASE visible_begin",
            "[1.000] Warning: aster-klog-console-visible",
            "KLOG_CONSOLE_PHASE visible_end retained_user=1",
            "KLOG_CONSOLE_PHASE reenabled_begin",
            "[1.000] Warning: aster-klog-console-visible-reenabled",
            "KLOG_CONSOLE_PHASE reenabled_end retained_user=1",
            "KLOG_CONSOLE_END",
            "KLOG_CONSOLE_CAPTURE kernel=1 hidden=1 visible=1",
            "KLOG_CONSOLE_EXIT=0",
            "KLOG_DMESG_BEGIN",
            "<6>[    1.500000] syscall_diag lifecycle=clone pid=42 tid=42 ppid=1",
            "KLOG_DMESG_EXIT=0",
            "dmesg from util-linux 2.41.5",
            "KLOG_UTIL_DMESG_EXIT=0",
            "KLOG_DMESG_FOLLOW stage=0 observed=1 reads=50 bytes=100",
            "KLOG_DMESG_FOLLOW stage=1 observed=1 reads=1 bytes=50",
            "KLOG_DMESG_FOLLOW_CHILD before_cleanup=running status=0",
            "KLOG_FOLLOW_EXIT=0",
            "KLOG_MICRO_END",
        ]
    )


class KlogMicroGateTests(unittest.TestCase):
    def test_accepts_complete_success(self):
        self.assertEqual(gate.validate(transcript()), 10)

    def test_capture_info_evidence_is_mandatory_and_only_in_dmesg_phase(self):
        for invalid in (
            transcript().replace("KLOG_CAPTURE_INFO_RETAINED=1\n", ""),
            transcript().replace(
                "KLOG_DMESG_BEGIN\n<6>[    1.500000] syscall_diag lifecycle=clone pid=42 tid=42 ppid=1",
                "<6>[    1.500000] syscall_diag lifecycle=clone pid=42 tid=42 ppid=1\nKLOG_DMESG_BEGIN",
            ),
        ):
            with self.assertRaises(ValueError):
                gate.validate(invalid)

    def test_follow_probe_uses_follow_new(self):
        source = FOLLOW_PROBE.read_text()
        self.assertIn('"--follow-new"', source)
        self.assertNotIn('"--follow", "--raw"', source)

    def test_old_buffer_drain_checkpoint_is_rejected(self):
        with self.assertRaises(ValueError):
            gate.validate(
                transcript().replace(
                    "KLOG_DMESG_FOLLOW stage=0",
                    "KLOG_DMESG_FOLLOW checkpoint=old_read_limit stage=0 reads=32 bytes=1\n"
                    "KLOG_DMESG_FOLLOW stage=0",
                )
            )

    def test_boot_arguments_keep_console_quiet_and_capture_info(self):
        self.assertEqual(
            gate.BOOT_APPEND,
            "init=/init loglevel=off asterinas.klog_capture=info "
            "asterinas.syscall_diag=1 console=ttyS0",
        )

    def test_missing_or_duplicate_marker_fails(self):
        for text in (
            transcript().replace("KLOG_MICRO_END", ""),
            transcript() + "\nKLOG_MICRO_END",
        ):
            with self.assertRaises(ValueError):
                gate.validate(text)

    def test_failed_or_missing_c_case_fails(self):
        for text in (
            transcript().replace("0 tests failed", "1 tests failed", 1),
            transcript().replace("test_action_validation", "test_unknown"),
        ):
            with self.assertRaises(ValueError):
                gate.validate(text)

    def test_follow_requires_two_acknowledged_messages_and_live_child(self):
        for text in (
            transcript().replace("stage=1 observed=1", "stage=1 observed=0"),
            transcript().replace("before_cleanup=running", "before_cleanup=exited"),
        ):
            with self.assertRaises(ValueError):
                gate.validate(text)

    def test_console_filter_is_distinct_from_capture(self):
        for text in (
            transcript().replace("aster-klog-console-visible", "absent"),
            transcript().replace(
                "KLOG_CONSOLE_END", "aster-klog-console-hidden\nKLOG_CONSOLE_END"
            ),
        ):
            with self.assertRaises(ValueError):
                gate.validate(text)
        # A later dmesg dump may legitimately contain the hidden record.
        gate.validate(
            transcript().replace(
                "KLOG_DMESG_EXIT", "aster-klog-console-hidden\nKLOG_DMESG_EXIT"
            )
        )

    def test_fatal_output_after_success_fails(self):
        for fatal in (
            "Uncaught panic",
            "Kernel panic",
            "Unexpected exception",
            "stack trace:",
        ):
            with self.assertRaises(ValueError):
                gate.validate(transcript() + "\n" + fatal)

    def test_console_phase_must_be_ordered(self):
        with self.assertRaises(ValueError):
            gate.validate(
                transcript()
                .replace("KLOG_CONSOLE_BEGIN", "temporary")
                .replace("KLOG_CONSOLE_END", "KLOG_CONSOLE_BEGIN")
                .replace("temporary", "KLOG_CONSOLE_END")
            )

    def test_conflicting_status_is_rejected(self):
        with self.assertRaises(ValueError):
            gate.validate(transcript() + "\nKLOG_MICRO_PROBE_EXIT=1")

    def test_evidence_cannot_be_outside_its_phase(self):
        original = transcript().splitlines()
        for prefix in ("test_action_validation", "KLOG_DMESG_FOLLOW stage=1"):
            line = next(line for line in original if line.startswith(prefix))
            remaining = [item for item in original if item != line]
            for invalid in ([line] + remaining, remaining + [line]):
                with self.assertRaises(ValueError):
                    gate.validate("\n".join(invalid))

    def test_follow_stages_must_be_ordered(self):
        with self.assertRaises(ValueError):
            gate.validate(
                transcript()
                .replace("stage=0", "stage=TEMP")
                .replace("stage=1", "stage=0")
                .replace("stage=TEMP", "stage=1")
            )

    def test_c_summary_lookalike_cannot_hide_out_of_phase_success(self):
        original = "test_action_validation summary: 1 tests passed, 0 tests failed"
        with self.assertRaises(ValueError):
            gate.validate(
                transcript().replace(original, original.replace("1 tests", "0 tests"))
                + "\n"
                + original
            )

    def test_console_reenable_requires_real_output(self):
        with self.assertRaises(ValueError):
            gate.validate(
                transcript().replace(
                    "[1.000] Warning: aster-klog-console-visible-reenabled", ""
                )
            )


if __name__ == "__main__":
    unittest.main()
