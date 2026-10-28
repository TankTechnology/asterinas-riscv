#!/usr/bin/env python3

from __future__ import annotations

from tools.riscv.tests.support.qemu_uboot import *


class AuditSerialLogTests(unittest.TestCase):
    @staticmethod
    def successful_marker_event() -> str:
        return "marker_seen=yes\naction=SIGTERM\ncleanup_complete=yes\n"

    @staticmethod
    def diagnostic_bootargs() -> str:
        return f"{MEGREZ_BOOTARGS} {FIRST_PROCESS_CONSOLE_LOSS.bootarg_suffix}"

    @staticmethod
    def registered_profile():
        return qemu_uboot_booti.profile_by_name("sifive-u-asterinas-smoke")

    def registered_serial_log(self) -> str:
        return MEGREZ_POSITIVE_SERIAL_LOG.replace(
            MEGREZ_BOOTARGS,
            self.registered_profile().bootargs,
        )

    def audit_registered_serial(self, serial_log: str):
        return qemu_uboot_booti.audit_serial_log(
            serial_log,
            marker_event=self.successful_marker_event(),
            profile=self.registered_profile(),
        )

    def assert_registered_failure(self, serial_log: str, failure: str) -> None:
        audit = self.audit_registered_serial(serial_log)
        self.assertFalse(audit.passed)
        self.assertIn(failure, audit.failures)

    def console_loss_serial_log(
        self,
        diagnostic_lines: tuple[str, ...] | list[str] = CONSOLE_LOSS_DIAGNOSTIC_LINES,
    ) -> str:
        diagnostic_output = "\n".join(diagnostic_lines)
        return MEGREZ_POSITIVE_SERIAL_LOG.replace(
            MEGREZ_BOOTARGS,
            self.diagnostic_bootargs(),
        ).replace(
            qemu_uboot_audit.USERSPACE_MARKER_TEXT,
            f"{PROCESS_STAGE_COMPLETE_LINE}\n{diagnostic_output}",
        )

    def suppression_serial_log(self) -> str:
        return MEGREZ_POSITIVE_SERIAL_LOG.replace(
            MEGREZ_BOOTARGS,
            self.diagnostic_bootargs(),
        ).replace(
            "OSTD initialized. Preparing components.\n",
            f"OSTD initialized. Preparing components.\n{UART_REGISTRATION_LOG}\n",
        )

    def audit_console_loss(
        self,
        diagnostic_lines: tuple[str, ...] | list[str] = CONSOLE_LOSS_DIAGNOSTIC_LINES,
        *,
        marker_event: str | None = None,
    ):
        return self.audit_console_loss_serial(
            self.console_loss_serial_log(diagnostic_lines),
            marker_event=marker_event,
        )

    def audit_console_loss_serial(
        self,
        serial_log: str,
        *,
        marker_event: str | None = None,
    ):
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        return qemu_uboot_booti.audit_serial_log(
            serial_log,
            marker_event=marker_event or self.successful_marker_event(),
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.FIRST_PROCESS_CONSOLE_LOSS,
            variant=FIRST_PROCESS_CONSOLE_LOSS,
        )

    def diagnostic_audit(
        self,
        lines: tuple[str, ...] | list[str],
        *,
        newline: str = "\n",
        terminated: bool = True,
        variant: QemuUbootVariant | None = FIRST_PROCESS_CONSOLE_LOSS,
        leading_line: str | None = None,
    ):
        transcript_lines = ([] if leading_line is None else [leading_line]) + list(
            lines
        )
        serial_log = newline.join(transcript_lines)
        if terminated:
            serial_log += newline
        return qemu_uboot_audit.audit_diagnostic_markers(
            serial_log,
            variant=variant,
        )

    @staticmethod
    def replace_diagnostic_stage(
        lines: tuple[str, ...], stage: str, replacement: str
    ) -> tuple[str, ...]:
        replaced = list(lines)
        index = REQUIRED_DIAGNOSTIC_STAGES.index(stage)
        replaced[index] = replacement
        return tuple(replaced)

    def test_accepts_complete_ordered_console_loss_diagnostics(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_audit, "DiagnosticAudit"))
        self.assertTrue(hasattr(qemu_uboot_audit, "audit_diagnostic_markers"))

        for newline in ("\n", "\r\n", "\r"):
            with self.subTest(newline=repr(newline)):
                colored_lines = tuple(
                    f"\x1b[32m{line}\x1b[0m" for line in COMPLETE_DIAGNOSTIC_LINES
                )
                audit = self.diagnostic_audit(
                    colored_lines,
                    newline=newline,
                    leading_line="ordinary boot output",
                )

                self.assertTrue(audit.passed, audit.failures)
                self.assertEqual(audit.total_count, 8)
                self.assertEqual(audit.activation_count, 1)
                self.assertEqual(audit.ordered_stages, REQUIRED_DIAGNOSTIC_STAGES)
                self.assertEqual(audit.last_stage, "user_first_write_returned")
                self.assertEqual(audit.first_return_reason, "user_syscall")
                self.assertEqual(audit.first_syscall, 64)
                self.assertEqual(audit.first_syscall_sepc, 0x1000)
                self.assertEqual(audit.write_fd, 1)
                self.assertEqual(audit.write_requested, 50)
                self.assertEqual(audit.write_result, 50)
                self.assertIsNone(audit.page_fault_outcome)
                self.assertFalse(audit.repeated_page_fault)
                evidence = {item.stage: item for item in audit.stage_evidence}
                self.assertEqual(tuple(evidence), ALL_DIAGNOSTIC_STAGES)
                for index, stage in enumerate(REQUIRED_DIAGNOSTIC_STAGES, start=1):
                    self.assertEqual(evidence[stage].count, 1)
                    self.assertEqual(evidence[stage].indices, (index,))
                for stage in (
                    "user_first_page_fault",
                    "user_first_page_fault_handler",
                    "user_page_fault_repeated",
                ):
                    self.assertEqual(evidence[stage].count, 0)
                    self.assertEqual(evidence[stage].indices, ())

        equal_copy = replace(FIRST_PROCESS_CONSOLE_LOSS)
        with self.assertRaisesRegex(ValueError, "registered singleton"):
            self.diagnostic_audit(COMPLETE_DIAGNOSTIC_LINES, variant=equal_copy)

    def test_each_legal_prefix_reports_its_last_valid_stage(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_audit, "DiagnosticAudit"))
        self.assertTrue(hasattr(qemu_uboot_audit, "audit_diagnostic_markers"))

        for length in range(len(REQUIRED_DIAGNOSTIC_STAGES) + 1):
            with self.subTest(length=length):
                prefix_lines = COMPLETE_DIAGNOSTIC_LINES[:length]
                audit = self.diagnostic_audit(
                    prefix_lines,
                    newline="\r\n",
                    leading_line="ordinary boot output",
                )
                expected_stages = REQUIRED_DIAGNOSTIC_STAGES[:length]
                expected_last = expected_stages[-1] if expected_stages else None
                evidence = {item.stage: item for item in audit.stage_evidence}

                self.assertEqual(audit.ordered_stages, expected_stages)
                self.assertEqual(audit.last_stage, expected_last)
                self.assertEqual(
                    audit.passed, length == len(REQUIRED_DIAGNOSTIC_STAGES)
                )
                for index, stage in enumerate(REQUIRED_DIAGNOSTIC_STAGES):
                    expected_indices = (index + 1,) if index < length else ()
                    self.assertEqual(evidence[stage].indices, expected_indices)
                    self.assertEqual(evidence[stage].count, int(index < length))

                if length <= 6:
                    disruption = COMPLETE_DIAGNOSTIC_LINES[length + 1]
                else:
                    disruption = COMPLETE_DIAGNOSTIC_LINES[length - 1]
                disrupted = self.diagnostic_audit(
                    (*prefix_lines, disruption),
                    leading_line="ordinary boot output",
                )
                self.assertFalse(disrupted.passed)
                self.assertEqual(disrupted.ordered_stages, expected_stages)
                self.assertEqual(disrupted.last_stage, expected_last)

    def test_every_marker_schema_requires_exact_complete_fields(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_audit, "audit_diagnostic_markers"))

        exception_details = {
            "instruction_misaligned": "detail_kind=unavailable",
            "instruction_fault": "detail_kind=unavailable",
            "illegal_instruction": "detail_kind=instruction detail=0x13",
            "breakpoint": "detail_kind=unavailable",
            "load_misaligned": "detail_kind=stval detail=0x2000",
            "load_fault": "detail_kind=stval detail=0x3000",
            "store_misaligned": "detail_kind=stval detail=0x4000",
            "store_fault": "detail_kind=stval detail=0x5000",
            "user_env_call": "detail_kind=unavailable",
            "supervisor_env_call": "detail_kind=unavailable",
            "instruction_page_fault": "detail_kind=stval detail=0x6000",
            "load_page_fault": "detail_kind=stval detail=0x7000",
            "store_page_fault": "detail_kind=stval detail=0x8000",
            "unknown": "detail_kind=unavailable",
        }
        return_lines = (
            f"{DIAGNOSTIC_PREFIX} stage=user_first_return reason=user_syscall sepc=0x1000",
            f"{DIAGNOSTIC_PREFIX} stage=user_first_return reason=kernel_event sepc=0x1000",
            *(
                f"{DIAGNOSTIC_PREFIX} stage=user_first_return "
                f"reason=user_exception sepc=0x1000 cause={cause} {detail}"
                for cause, detail in exception_details.items()
            ),
        )
        for line in return_lines:
            with self.subTest(valid_return=line):
                lines = self.replace_diagnostic_stage(
                    COMPLETE_DIAGNOSTIC_LINES,
                    "user_first_return",
                    line,
                )
                audit = self.diagnostic_audit(lines)
                self.assertTrue(audit.passed, audit.failures)
                self.assertEqual(
                    audit.first_return_reason,
                    line.split(" reason=", 1)[1].split(" ", 1)[0],
                )

        for cause in (
            "instruction_page_fault",
            "load_page_fault",
            "store_page_fault",
        ):
            for outcome in ("resolved", "fault_signal_queued"):
                with self.subTest(page_fault_cause=cause, outcome=outcome):
                    lines = list(COMPLETE_DIAGNOSTIC_LINES)
                    lines[6:6] = [
                        f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault "
                        f"cause={cause} stval=0x4000 sepc=0x1000",
                        f"{DIAGNOSTIC_PREFIX} "
                        f"stage=user_first_page_fault_handler outcome={outcome}",
                        f"{DIAGNOSTIC_PREFIX} stage=user_page_fault_repeated "
                        f"cause={cause} stval=0x4000 sepc=0x1000",
                    ]
                    audit = self.diagnostic_audit(lines)
                    self.assertTrue(audit.passed, audit.failures)
                    self.assertEqual(audit.page_fault_outcome, outcome)
                    self.assertTrue(audit.repeated_page_fault)

        malformed_required = {
            "diagnostic_active": (f"{DIAGNOSTIC_PREFIX} stage=diagnostic_active"),
            "process_components_ready": (
                f"{DIAGNOSTIC_PREFIX} stage=process_components_ready extra=1"
            ),
            "device_init_ready": (
                f"{DIAGNOSTIC_PREFIX} stage=device_init_ready extra=1"
            ),
            "stdio_init_ready": (f"{DIAGNOSTIC_PREFIX} stage=stdio_init_ready extra=1"),
            "user_enter": (
                f"{DIAGNOSTIC_PREFIX} stage=user_enter cpu=+0 sepc=0x1000 sp=0x2000"
            ),
            "user_first_return": (
                f"{DIAGNOSTIC_PREFIX} stage=user_first_return "
                "reason=user_syscall sepc=0x1000 cause=breakpoint"
            ),
            "user_first_syscall": (
                f"{DIAGNOSTIC_PREFIX} stage=user_first_syscall id=0x40 sepc=0x1000"
            ),
            "user_first_write_returned": (
                f"{DIAGNOSTIC_PREFIX} stage=user_first_write_returned "
                "fd=1 requested=50 result=+50"
            ),
        }
        for stage, malformed in malformed_required.items():
            with self.subTest(malformed_stage=stage):
                lines = self.replace_diagnostic_stage(
                    COMPLETE_DIAGNOSTIC_LINES,
                    stage,
                    malformed,
                )
                audit = self.diagnostic_audit(lines)
                evidence = {item.stage: item for item in audit.stage_evidence}
                self.assertFalse(audit.passed)
                self.assertEqual(evidence[stage].count, 0)

        invalid_exception_lines = (
            f"{DIAGNOSTIC_PREFIX} stage=user_first_return reason=unknown sepc=0x1000",
            f"{DIAGNOSTIC_PREFIX} stage=user_first_return reason=user_exception "
            "sepc=0x1000 cause=illegal_instruction detail_kind=stval detail=0x13",
            f"{DIAGNOSTIC_PREFIX} stage=user_first_return reason=user_exception "
            "sepc=0x1000 cause=breakpoint detail_kind=unavailable detail=0x0",
            f"{DIAGNOSTIC_PREFIX} stage=user_first_return reason=user_exception "
            "sepc=0x1000 cause=load_fault detail_kind=stval detail=0X3000",
        )
        for malformed in invalid_exception_lines:
            with self.subTest(invalid_exception=malformed):
                lines = self.replace_diagnostic_stage(
                    COMPLETE_DIAGNOSTIC_LINES,
                    "user_first_return",
                    malformed,
                )
                self.assertFalse(self.diagnostic_audit(lines).passed)

        malformed_optional = (
            f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault "
            "cause=unknown stval=0x4000 sepc=0x1000",
            f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault_handler "
            "outcome=resolved extra=1",
            f"{DIAGNOSTIC_PREFIX} stage=user_page_fault_repeated "
            "cause=load_page_fault sepc=0x1000 stval=0x4000",
            f"{DIAGNOSTIC_PREFIX} stage=unknown_stage value=1",
        )
        for malformed in malformed_optional:
            with self.subTest(malformed_optional=malformed):
                lines = list(COMPLETE_DIAGNOSTIC_LINES)
                lines.insert(6, malformed)
                self.assertFalse(self.diagnostic_audit(lines).passed)

        unterminated = self.diagnostic_audit(
            COMPLETE_DIAGNOSTIC_LINES,
            terminated=False,
        )
        self.assertFalse(unterminated.passed)
        self.assertEqual(unterminated.last_stage, "user_first_syscall")
        evidence = {item.stage: item for item in unterminated.stage_evidence}
        self.assertEqual(evidence["user_first_write_returned"].count, 0)

    def test_marker_free_normal_log_reports_no_last_stage(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_audit, "audit_diagnostic_markers"))

        clean = qemu_uboot_audit.audit_diagnostic_markers(
            CLEAN_SERIAL_LOG,
            variant=None,
        )
        self.assertTrue(clean.passed, clean.failures)
        self.assertEqual(clean.total_count, 0)
        self.assertEqual(clean.activation_count, 0)
        self.assertEqual(clean.ordered_stages, ())
        self.assertIsNone(clean.last_stage)

        with_marker = CLEAN_SERIAL_LOG.replace(
            "Enter riscv_boot\n",
            "Enter riscv_boot\n" + COMPLETE_DIAGNOSTIC_LINES[0] + "\n",
        )
        rejected = qemu_uboot_audit.audit_diagnostic_markers(
            with_marker,
            variant=None,
        )
        self.assertFalse(rejected.passed)
        self.assertEqual(rejected.total_count, 1)
        self.assertEqual(rejected.activation_count, 1)
        self.assertIsNone(rejected.last_stage)

    def test_rejects_malformed_diagnostic_prefix_candidates(self) -> None:
        malformed_candidates = (
            f"{DIAGNOSTIC_PREFIX}X stage=diagnostic_active console_registry=empty",
            f"{DIAGNOSTIC_PREFIX}\tstage=diagnostic_active console_registry=empty",
            f"{DIAGNOSTIC_PREFIX}stage=diagnostic_active console_registry=empty",
        )

        for candidate in malformed_candidates:
            with self.subTest(candidate=candidate, variant="none"):
                audit = self.diagnostic_audit((candidate,), variant=None)
                self.assertEqual(audit.total_count, 1)
                self.assertFalse(audit.passed)
                self.assertIsNone(audit.last_stage)

            with self.subTest(candidate=candidate, variant="registered"):
                audit = self.diagnostic_audit((*COMPLETE_DIAGNOSTIC_LINES, candidate))
                self.assertEqual(audit.total_count, 9)
                self.assertFalse(audit.passed)
                self.assertEqual(audit.last_stage, "user_first_write_returned")

    def test_rejects_oversized_diagnostic_numbers(self) -> None:
        oversized = "9" * 5000
        cases = (
            (
                "user_first_syscall",
                "id=64",
                f"id={oversized}",
                "user_first_return",
            ),
            (
                "user_first_write_returned",
                "fd=1",
                f"fd={oversized}",
                "user_first_syscall",
            ),
            (
                "user_first_write_returned",
                "requested=50",
                f"requested={oversized}",
                "user_first_syscall",
            ),
            (
                "user_first_write_returned",
                "result=50",
                f"result={oversized}",
                "user_first_syscall",
            ),
            (
                "user_first_write_returned",
                "result=50",
                f"result=-{oversized}",
                "user_first_syscall",
            ),
        )

        for stage, original, replacement, expected_last in cases:
            with self.subTest(stage=stage, replacement=replacement[:24]):
                valid_line = COMPLETE_DIAGNOSTIC_LINES[
                    REQUIRED_DIAGNOSTIC_STAGES.index(stage)
                ]
                lines = self.replace_diagnostic_stage(
                    COMPLETE_DIAGNOSTIC_LINES,
                    stage,
                    valid_line.replace(original, replacement),
                )
                audit = self.diagnostic_audit(lines)
                evidence = {item.stage: item for item in audit.stage_evidence}

                self.assertFalse(audit.passed)
                self.assertEqual(evidence[stage].count, 0)
                self.assertEqual(audit.last_stage, expected_last)

    def test_rejects_missing_duplicate_reordered_and_truncated_markers(self) -> None:
        for index, stage in enumerate(REQUIRED_DIAGNOSTIC_STAGES):
            with self.subTest(disruption="missing", stage=stage):
                lines = list(COMPLETE_DIAGNOSTIC_LINES)
                lines.pop(index)
                audit = self.diagnostic_audit(lines)
                expected_prefix = REQUIRED_DIAGNOSTIC_STAGES[:index]
                self.assertFalse(audit.passed)
                self.assertEqual(audit.ordered_stages, expected_prefix)
                self.assertEqual(
                    audit.last_stage,
                    expected_prefix[-1] if expected_prefix else None,
                )

            with self.subTest(disruption="duplicate", stage=stage):
                lines = list(COMPLETE_DIAGNOSTIC_LINES)
                lines.insert(index + 1, lines[index])
                audit = self.diagnostic_audit(lines)
                expected_prefix = REQUIRED_DIAGNOSTIC_STAGES[: index + 1]
                self.assertFalse(audit.passed)
                self.assertEqual(audit.ordered_stages, expected_prefix)
                self.assertEqual(audit.last_stage, expected_prefix[-1])

        for index in range(len(REQUIRED_DIAGNOSTIC_STAGES) - 1):
            with self.subTest(disruption="reordered", index=index):
                lines = list(COMPLETE_DIAGNOSTIC_LINES)
                lines[index], lines[index + 1] = lines[index + 1], lines[index]
                audit = self.diagnostic_audit(lines)
                expected_prefix = REQUIRED_DIAGNOSTIC_STAGES[:index]
                self.assertFalse(audit.passed)
                self.assertEqual(audit.ordered_stages, expected_prefix)
                self.assertEqual(
                    audit.last_stage,
                    expected_prefix[-1] if expected_prefix else None,
                )

        truncated = self.diagnostic_audit(
            COMPLETE_DIAGNOSTIC_LINES,
            terminated=False,
        )
        self.assertFalse(truncated.passed)
        self.assertEqual(
            truncated.ordered_stages,
            REQUIRED_DIAGNOSTIC_STAGES[:-1],
        )
        self.assertEqual(truncated.last_stage, "user_first_syscall")

    def test_rejects_malformed_fields_for_every_marker_kind(self) -> None:
        required_cases = (
            (
                "diagnostic_active",
                "stage=diagnostic_active console_registry=occupied",
            ),
            ("process_components_ready", "stage=process_components_ready extra=1"),
            ("device_init_ready", "stage=device_init_ready extra=1"),
            ("stdio_init_ready", "stage=stdio_init_ready extra=1"),
            ("user_enter", "stage=user_enter cpu=-1 sepc=0x1000 sp=0x2000"),
            ("user_enter", "stage=user_enter cpu=0 sepc=1000 sp=0x2000"),
            ("user_enter", "stage=user_enter cpu=0 sepc=0x1000 sp=0X2000"),
            (
                "user_first_return",
                "stage=user_first_return reason=user_syscall sepc=0X1000",
            ),
            (
                "user_first_return",
                "stage=user_first_return reason=kernel_event sepc=1000",
            ),
            (
                "user_first_return",
                "stage=user_first_return reason=user_exception sepc=0x1000 "
                "cause=not_a_cause detail_kind=unavailable",
            ),
            ("user_first_syscall", "stage=user_first_syscall id=+56 sepc=0x1000"),
            ("user_first_syscall", "stage=user_first_syscall id=56 sepc=0X1000"),
            (
                "user_first_write_returned",
                "stage=user_first_write_returned fd=-1 requested=50 result=50",
            ),
            (
                "user_first_write_returned",
                "stage=user_first_write_returned fd=1 requested=+50 result=50",
            ),
            (
                "user_first_write_returned",
                "stage=user_first_write_returned fd=1 requested=50 result=0x32",
            ),
        )
        for stage, fields in required_cases:
            with self.subTest(stage=stage, fields=fields):
                malformed = f"{DIAGNOSTIC_PREFIX} {fields}"
                lines = self.replace_diagnostic_stage(
                    COMPLETE_DIAGNOSTIC_LINES,
                    stage,
                    malformed,
                )
                audit = self.diagnostic_audit(lines)
                stage_index = REQUIRED_DIAGNOSTIC_STAGES.index(stage)
                expected_prefix = REQUIRED_DIAGNOSTIC_STAGES[:stage_index]
                self.assertFalse(audit.passed)
                self.assertEqual(audit.ordered_stages, expected_prefix)
                self.assertEqual(
                    audit.last_stage,
                    expected_prefix[-1] if expected_prefix else None,
                )

        valid_fault = (
            f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault "
            "cause=load_page_fault stval=0x4000 sepc=0x1000"
        )
        valid_handler = (
            f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault_handler outcome=resolved"
        )
        optional_cases = (
            (
                (),
                f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault "
                "cause=unknown stval=0x4000 sepc=0x1000",
            ),
            (
                (),
                f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault "
                "cause=load_page_fault stval=0X4000 sepc=0x1000",
            ),
            (
                (),
                f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault "
                "cause=load_page_fault stval=0x4000 sepc=1000",
            ),
            (
                (valid_fault,),
                f"{DIAGNOSTIC_PREFIX} "
                "stage=user_first_page_fault_handler outcome=ignored",
            ),
            (
                (valid_fault, valid_handler),
                f"{DIAGNOSTIC_PREFIX} stage=user_page_fault_repeated "
                "cause=load_page_fault stval=4000 sepc=0x1000",
            ),
        )
        for prefix, malformed in optional_cases:
            with self.subTest(optional=malformed):
                lines = list(COMPLETE_DIAGNOSTIC_LINES)
                lines[6:6] = [*prefix, malformed]
                audit = self.diagnostic_audit(lines)
                self.assertFalse(audit.passed)
                self.assertEqual(
                    audit.ordered_stages,
                    REQUIRED_DIAGNOSTIC_STAGES[:6],
                )
                self.assertEqual(audit.last_stage, "user_first_return")

    def test_rejects_unknown_extra_or_misordered_fields(self) -> None:
        cases = (
            (
                "diagnostic_active",
                f"{DIAGNOSTIC_PREFIX} stage=diagnostic_active "
                "console_registry=empty unknown=1",
            ),
            (
                "user_enter",
                f"{DIAGNOSTIC_PREFIX} stage=user_enter cpu=0 sp=0x2000 sepc=0x1000",
            ),
            (
                "user_first_return",
                f"{DIAGNOSTIC_PREFIX} stage=user_first_return "
                "sepc=0x1000 reason=user_syscall",
            ),
            (
                "user_first_syscall",
                f"{DIAGNOSTIC_PREFIX} stage=user_first_syscall sepc=0x1000 id=56",
            ),
            (
                "user_first_write_returned",
                f"{DIAGNOSTIC_PREFIX} stage=user_first_write_returned "
                "fd=1 result=50 requested=50",
            ),
            (
                "user_first_write_returned",
                f"{DIAGNOSTIC_PREFIX} stage=user_first_write_returned "
                "fd=1 requested=50 result=50 extra=1",
            ),
        )
        for stage, malformed in cases:
            with self.subTest(stage=stage, malformed=malformed):
                lines = self.replace_diagnostic_stage(
                    COMPLETE_DIAGNOSTIC_LINES,
                    stage,
                    malformed,
                )
                audit = self.diagnostic_audit(lines)
                stage_index = REQUIRED_DIAGNOSTIC_STAGES.index(stage)
                self.assertFalse(audit.passed)
                self.assertEqual(
                    audit.ordered_stages,
                    REQUIRED_DIAGNOSTIC_STAGES[:stage_index],
                )

        unknown_stage = list(COMPLETE_DIAGNOSTIC_LINES)
        unknown_stage.insert(6, f"{DIAGNOSTIC_PREFIX} stage=unknown_stage value=1")
        audit = self.diagnostic_audit(unknown_stage)
        self.assertFalse(audit.passed)
        self.assertEqual(
            audit.ordered_stages,
            REQUIRED_DIAGNOSTIC_STAGES[:6],
        )

    def test_rejects_invalid_return_reason_detail_coupling(self) -> None:
        invalid_return_fields = (
            "reason=unknown sepc=0x1000",
            "reason=user_syscall sepc=0x1000 cause=breakpoint",
            "reason=kernel_event sepc=0x1000 detail_kind=unavailable",
            "reason=user_exception sepc=0x1000 cause=illegal_instruction "
            "detail_kind=instruction",
            "reason=user_exception sepc=0x1000 cause=illegal_instruction "
            "detail_kind=stval detail=0x13",
            "reason=user_exception sepc=0x1000 cause=breakpoint "
            "detail_kind=unavailable detail=0x0",
            "reason=user_exception sepc=0x1000 cause=load_fault "
            "detail_kind=unavailable",
            "reason=user_exception sepc=0x1000 cause=load_fault "
            "detail_kind=stval detail=0X3000",
        )
        for fields in invalid_return_fields:
            with self.subTest(fields=fields):
                malformed = f"{DIAGNOSTIC_PREFIX} stage=user_first_return {fields}"
                lines = self.replace_diagnostic_stage(
                    COMPLETE_DIAGNOSTIC_LINES,
                    "user_first_return",
                    malformed,
                )
                audit = self.diagnostic_audit(lines)
                self.assertFalse(audit.passed)
                self.assertEqual(
                    audit.ordered_stages,
                    REQUIRED_DIAGNOSTIC_STAGES[:5],
                )
                self.assertEqual(audit.last_stage, "user_enter")
                self.assertIsNone(audit.first_return_reason)

    def test_accepts_only_a_complete_resolved_page_fault_pair(self) -> None:
        no_fault = self.audit_console_loss()
        self.assertTrue(no_fault.passed, no_fault.failures)
        self.assertEqual(
            no_fault.classification,
            FIRST_PROCESS_CONSOLE_LOSS.classification,
        )
        self.assertEqual(no_fault.effective_bootargs, self.diagnostic_bootargs())
        self.assertTrue(no_fault.diagnostic.passed, no_fault.diagnostic.failures)
        self.assertIsNone(no_fault.diagnostic.page_fault_outcome)

        resolved_lines = list(CONSOLE_LOSS_DIAGNOSTIC_LINES)
        resolved_lines[6:6] = [
            f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault "
            "cause=load_page_fault stval=0x4000 sepc=0x1000",
            f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault_handler outcome=resolved",
        ]
        resolved = self.audit_console_loss(resolved_lines)
        self.assertTrue(resolved.passed, resolved.failures)
        self.assertEqual(resolved.diagnostic.page_fault_outcome, "resolved")
        self.assertFalse(resolved.diagnostic.repeated_page_fault)

        process_after_activation = self.console_loss_serial_log().replace(
            f"{PROCESS_STAGE_COMPLETE_LINE}\n{CONSOLE_LOSS_DIAGNOSTIC_LINES[0]}",
            f"{CONSOLE_LOSS_DIAGNOSTIC_LINES[0]}\n{PROCESS_STAGE_COMPLETE_LINE}",
        )
        rejected = self.audit_console_loss_serial(process_after_activation)
        self.assertFalse(rejected.passed)

    def test_rejects_queued_repeated_or_incomplete_page_fault_evidence(self) -> None:
        fault = (
            f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault "
            "cause=load_page_fault stval=0x4000 sepc=0x1000"
        )
        resolved = (
            f"{DIAGNOSTIC_PREFIX} stage=user_first_page_fault_handler outcome=resolved"
        )
        queued = (
            f"{DIAGNOSTIC_PREFIX} "
            "stage=user_first_page_fault_handler outcome=fault_signal_queued"
        )
        repeated = (
            f"{DIAGNOSTIC_PREFIX} stage=user_page_fault_repeated "
            "cause=load_page_fault stval=0x4000 sepc=0x1000"
        )
        cases = (
            (fault, queued),
            (fault, resolved, repeated),
            (fault,),
            (resolved,),
            (fault, repeated),
        )
        for optional_lines in cases:
            with self.subTest(optional_lines=optional_lines):
                lines = list(CONSOLE_LOSS_DIAGNOSTIC_LINES)
                lines[6:6] = optional_lines
                audit = self.audit_console_loss(lines)
                self.assertFalse(audit.passed)

    def test_rejects_wrong_first_syscall_or_write_result(self) -> None:
        mutations = (
            ("id=56", "id=64"),
            ("fd=1", "fd=2"),
            ("requested=50", "requested=49"),
            ("result=50", "result=49"),
            ("result=50", "result=-5"),
        )
        for original, replacement in mutations:
            with self.subTest(replacement=replacement):
                lines = tuple(
                    line.replace(original, replacement)
                    for line in CONSOLE_LOSS_DIAGNOSTIC_LINES
                )
                audit = self.audit_console_loss(lines)
                self.assertFalse(audit.passed)

        bad_marker_events = (
            "marker_seen=no\naction=SIGTERM\ncleanup_complete=yes\n",
            "marker_seen=yes\naction=already-exited\ncleanup_complete=yes\n",
            "marker_seen=yes\naction=SIGTERM\ncleanup_complete=no\n",
            "marker_seen=yes\naction=SIGTERM\ncleanup_complete=yes\n"
            "failure=boot-timeout\n",
            "marker_seen=yes\naction=SIGKILL\ncleanup_complete=yes\n"
            "failure=session-error\n",
        )
        for marker_event in bad_marker_events:
            with self.subTest(marker_event=marker_event):
                audit = self.audit_console_loss(marker_event=marker_event)
                self.assertFalse(audit.passed)

    def test_normal_and_stale_reject_any_diagnostic_prefix(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        injected_positive = MEGREZ_POSITIVE_SERIAL_LOG.replace(
            "Enter riscv_boot\n",
            f"Enter riscv_boot\n{CONSOLE_LOSS_DIAGNOSTIC_LINES[0]}\n",
        )
        positive = qemu_uboot_booti.audit_serial_log(
            injected_positive,
            marker_event=self.successful_marker_event(),
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.POSITIVE,
        )
        self.assertFalse(positive.passed)
        self.assertGreater(positive.diagnostic.total_count, 0)

        injected_stale = MEGREZ_STALE_SERIAL_LOG.replace(
            "Enter riscv_boot\n",
            f"Enter riscv_boot\n{DIAGNOSTIC_PREFIX} malformed\n",
        )
        stale = qemu_uboot_booti.audit_serial_log(
            injected_stale,
            marker_event=(
                "marker_seen=no\naction=SIGTERM\nfailure=boot-timeout\n"
                "cleanup_complete=yes\n"
            ),
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
        )
        self.assertFalse(stale.passed)
        self.assertGreater(stale.diagnostic.total_count, 0)

    def test_registered_console_suppression_requires_flag_ns16550_and_zero_diagnostics(
        self,
    ) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")

        def audit(serial_log: str):
            return qemu_uboot_booti.audit_serial_log(
                serial_log,
                marker_event=self.successful_marker_event(),
                profile=profile,
                scenario=(qemu_uboot_booti.BootScenario.REGISTERED_CONSOLE_SUPPRESSION),
            )

        valid = audit(self.suppression_serial_log())
        self.assertTrue(valid.passed, valid.failures)
        self.assertEqual(valid.classification, "PASS")
        self.assertEqual(valid.effective_bootargs, self.diagnostic_bootargs())
        self.assertEqual(valid.userspace_marker_count, 1)
        self.assertEqual(valid.diagnostic.total_count, 0)

        mutations = (
            self.suppression_serial_log().replace(
                f" {FIRST_PROCESS_CONSOLE_LOSS.bootarg_suffix}",
                "",
            ),
            self.suppression_serial_log().replace(
                f"{UART_REGISTRATION_LOG}\n",
                "",
            ),
            self.suppression_serial_log().replace(
                f"{UART_REGISTRATION_LOG}\n",
                f"{UART_REGISTRATION_LOG}\n" * 2,
            ),
            self.suppression_serial_log().replace(
                qemu_uboot_audit.USERSPACE_MARKER_TEXT,
                "",
            ),
            self.suppression_serial_log().replace(
                "Enter riscv_boot\n",
                f"Enter riscv_boot\n{CONSOLE_LOSS_DIAGNOSTIC_LINES[0]}\n",
            ),
        )
        for serial_log in mutations:
            with self.subTest(serial_log=serial_log[-300:]):
                self.assertFalse(audit(serial_log).passed)

    def test_console_loss_rejects_real_uart_registration_log(self) -> None:
        serial_log = self.console_loss_serial_log().replace(
            "OSTD initialized. Preparing components.\n",
            f"OSTD initialized. Preparing components.\n{UART_REGISTRATION_LOG}\n",
        )

        audit = self.audit_console_loss_serial(serial_log)

        self.assertFalse(audit.passed)
        self.assertIn(
            "console-loss unexpectedly registered an NS16550 console",
            audit.failures,
        )

    def test_accepts_one_complete_marker_terminated_boot(self) -> None:
        self.assertTrue(hasattr(qemu_uboot_booti, "audit_serial_log"))

        audit = qemu_uboot_booti.audit_serial_log(
            CLEAN_SERIAL_LOG,
            marker_event=self.successful_marker_event(),
        )

        self.assertEqual(audit.booti_command_count, 1)
        self.assertEqual(audit.userspace_marker_count, 1)
        self.assertEqual(audit.application_processor_ids, ())
        self.assertIsNone(audit.random_source)
        self.assertEqual(audit.classification, "PASS")
        self.assertTrue(audit.passed)

    def test_accepts_the_serial_interaction_ack_as_the_userspace_marker(self) -> None:
        ready = RX_READY_LINE.decode()
        ack = RX_ACK_LINE.decode()
        serial_log = CLEAN_SERIAL_LOG.replace(
            qemu_uboot_audit.USERSPACE_MARKER_TEXT,
            f"{ready}\n{ack}",
        )

        audit = qemu_uboot_booti.audit_serial_log(
            serial_log,
            marker_event=self.successful_marker_event(),
            userspace_marker=ack,
            readiness_marker=ready,
        )

        self.assertEqual(audit.userspace_marker_count, 1)
        self.assertTrue(audit.passed, audit.failures)

        duplicate_ready = qemu_uboot_booti.audit_serial_log(
            serial_log.replace(ready, f"{ready}\n{ready}"),
            marker_event=self.successful_marker_event(),
            userspace_marker=ack,
            readiness_marker=ready,
        )
        self.assertFalse(duplicate_ready.passed)

    def test_accepts_each_megrez_ad_envelope_and_normalizes_ap_order(self) -> None:
        for profile_name, extensions in (
            ("megrez-sv48-svade-fast", "sstc,svade"),
            ("megrez-sv48-svadu-fast", "sstc,svadu"),
        ):
            with self.subTest(profile=profile_name):
                profile = qemu_uboot_booti.profile_by_name(profile_name)
                serial_log = MEGREZ_POSITIVE_SERIAL_LOG.replace(
                    "sstc,svade", extensions
                )

                audit = qemu_uboot_booti.audit_serial_log(
                    serial_log,
                    marker_event=self.successful_marker_event(),
                    profile=profile,
                    scenario=qemu_uboot_booti.BootScenario.POSITIVE,
                )

                self.assertTrue(audit.passed, audit.failures)
                self.assertEqual(audit.application_processor_ids, (1, 2, 3))
                self.assertEqual(audit.random_source, "timestamp")
                self.assertEqual(audit.classification, "PASS")

    def test_rejects_megrez_smp_rng_bootargs_and_seed_regressions(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        mutations = (
            MEGREZ_POSITIVE_SERIAL_LOG.replace(
                "Processor 3 started. Spinning for tasks.\n", ""
            ),
            MEGREZ_POSITIVE_SERIAL_LOG.replace(
                "use randomness based on the timestamp, which is insecure",
                "use randomness generated by hardware",
            ),
            MEGREZ_POSITIVE_SERIAL_LOG.replace(
                "use randomness based on the timestamp, which is insecure",
                "use randomness provided by the device tree",
            ),
            MEGREZ_POSITIVE_SERIAL_LOG.replace("sstc,svade", "sstc,zkr,svade"),
            MEGREZ_POSITIVE_SERIAL_LOG.replace(
                f'    bootargs = "{MEGREZ_BOOTARGS}";',
                f'    bootargs = "{MEGREZ_BOOTARGS}";\n    rng-seed = [01 02];',
            ),
            MEGREZ_POSITIVE_SERIAL_LOG.replace(
                MEGREZ_BOOTARGS,
                "console=ttyS0 loglevel=info init=/init",
            ),
        )

        for serial_log in mutations:
            with self.subTest(serial_log=serial_log):
                audit = qemu_uboot_booti.audit_serial_log(
                    serial_log,
                    marker_event=self.successful_marker_event(),
                    profile=profile,
                )
                self.assertFalse(audit.passed)

    def test_classifies_the_exact_stale_bootargs_failure_as_expected(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        audit = qemu_uboot_booti.audit_serial_log(
            MEGREZ_STALE_SERIAL_LOG,
            marker_event=(
                "marker_seen=no\naction=SIGTERM\nfailure=boot-timeout\n"
                "cleanup_complete=yes\n"
            ),
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
        )

        self.assertTrue(audit.passed, audit.failures)
        self.assertEqual(audit.userspace_marker_count, 0)
        self.assertEqual(audit.classification, "EXPECTED_INIT_ENOENT")

    def test_stale_bootargs_requires_exact_panic_and_timeout_cleanup(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        cases = (
            (
                MEGREZ_STALE_SERIAL_LOG.replace("found a negative dentry", "missing"),
                "marker_seen=no\naction=SIGTERM\nfailure=boot-timeout\n"
                "cleanup_complete=yes\n",
            ),
            (
                MEGREZ_STALE_SERIAL_LOG,
                "marker_seen=no\naction=SIGTERM\nfailure=boot-timeout\n"
                "cleanup_complete=no\n",
            ),
            (
                MEGREZ_STALE_SERIAL_LOG
                + ">>> Hello from RISC-V userspace on Asterinas! <<<\n",
                "marker_seen=yes\naction=SIGTERM\nfailure=boot-timeout\n"
                "cleanup_complete=yes\n",
            ),
            (
                MEGREZ_STALE_SERIAL_LOG + "secondary panic after init failure\n",
                "marker_seen=no\naction=SIGTERM\nfailure=boot-timeout\n"
                "cleanup_complete=yes\n",
            ),
        )
        for serial_log, marker_event in cases:
            with self.subTest(marker_event=marker_event):
                audit = qemu_uboot_booti.audit_serial_log(
                    serial_log,
                    marker_event=marker_event,
                    profile=profile,
                    scenario=qemu_uboot_booti.BootScenario.STALE_BOOTARGS,
                )
                self.assertFalse(audit.passed)

    def test_positive_scenario_still_rejects_the_stale_panic(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svade-fast")
        audit = qemu_uboot_booti.audit_serial_log(
            MEGREZ_STALE_SERIAL_LOG,
            marker_event="marker_seen=no\naction=SIGTERM\n",
            profile=profile,
            scenario=qemu_uboot_booti.BootScenario.POSITIVE,
        )

        self.assertFalse(audit.passed)

    def test_registered_milestones_accept_expected_command_echoes(self) -> None:
        audit = self.audit_registered_serial(self.registered_serial_log())

        self.assertTrue(audit.passed, audit.failures)

    def test_registered_tcp_probe_accepts_an_interleaved_exact_terminal_token(
        self,
    ) -> None:
        profile = qemu_uboot_booti.profile_by_name(
            "generic-sv39-smp4-tcp-probe"
        )
        terminal = profile.validation.completion_line.decode()
        serial_log = MEGREZ_POSITIVE_SERIAL_LOG.replace(
            MEGREZ_BOOTARGS, profile.bootargs
        ).replace(
            qemu_uboot_audit.USERSPACE_MARKER_TEXT,
            f"virtio interrupt prefix {terminal} kernel suffix",
        )

        audit = qemu_uboot_booti.audit_serial_log(
            serial_log,
            marker_event=self.successful_marker_event(),
            profile=profile,
            userspace_marker=terminal,
        )

        self.assertTrue(audit.passed, audit.failures)

    def test_registered_milestones_reject_a_reboot_after_the_terminal(self) -> None:
        self.assert_registered_failure(
            self.registered_serial_log() + "OpenSBI v1.7\n",
            "milestone repeated after terminal: FirmwareReady",
        )

    def test_registered_recovery_accepts_one_fresh_firmware_epoch(self) -> None:
        profile = qemu_uboot_booti.profile_by_name(
            "generic-sv39-smp4-software-reboot"
        )
        serial_log = (
            MEGREZ_POSITIVE_SERIAL_LOG.replace(MEGREZ_BOOTARGS, profile.bootargs)
            .replace(
                qemu_uboot_audit.USERSPACE_MARKER_TEXT,
                (
                    "ASTERINAS_SOFTWARE_REBOOT_ARMED seconds=60\n"
                    + profile.validation.completion_line.decode()
                ),
            )
            + "OpenSBI v1.7\nU-Boot 2026.07\n=> \n"
        )

        audit = qemu_uboot_booti.audit_serial_log(
            serial_log,
            marker_event=self.successful_marker_event(),
            profile=profile,
            userspace_marker=profile.validation.completion_line.decode(),
        )

        self.assertTrue(audit.passed, audit.failures)
        mutations = (
            serial_log.replace(
                "ASTERINAS_SOFTWARE_REBOOT_ARMED seconds=60\n",
                "",
            ),
            (
                serial_log.rsplit("OpenSBI v1.7\n", 1)[0]
                + "U-Boot 2026.07\nOpenSBI v1.7\n=> \n"
            ),
            serial_log.rsplit("=> \n", 1)[0],
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation[-160:]):
                rejected = qemu_uboot_booti.audit_serial_log(
                    mutation,
                    marker_event=self.successful_marker_event(),
                    profile=profile,
                    userspace_marker=profile.validation.completion_line.decode(),
                )
                self.assertFalse(rejected.passed)

    def test_registered_recovery_preserves_terminal_after_partial_ansi_prefix(
        self,
    ) -> None:
        profile = qemu_uboot_booti.profile_by_name(
            "generic-sv39-smp4-software-reboot"
        )
        terminal = profile.validation.completion_line.decode()
        serial_log = (
            MEGREZ_POSITIVE_SERIAL_LOG.replace(MEGREZ_BOOTARGS, profile.bootargs)
            .replace(
                qemu_uboot_audit.USERSPACE_MARKER_TEXT,
                "ASTERINAS_SOFTWARE_REBOOT_ARMED seconds=60\n\x1b[" + terminal,
            )
            + "OpenSBI v1.7\nU-Boot 2026.07\n=> \n"
        )

        audit = qemu_uboot_booti.audit_serial_log(
            serial_log,
            marker_event=self.successful_marker_event(),
            profile=profile,
            userspace_marker=terminal,
        )

        self.assertTrue(audit.passed, audit.failures)

    def test_registered_milestones_reject_a_reboot_before_the_successful_chain(
        self,
    ) -> None:
        serial_log = "OpenSBI v1.7\nU-Boot 2026.07\n" + self.registered_serial_log()

        self.assert_registered_failure(
            serial_log,
            "unexpected milestone occurrence count: FirmwareReady expected 1, got 2",
        )

    def test_registered_milestones_reject_repeated_firmware_before_bootloader(
        self,
    ) -> None:
        serial_log = self.registered_serial_log().replace(
            "OpenSBI v1.7\n",
            "OpenSBI v1.7\nOpenSBI v1.7\n",
            1,
        )

        self.assert_registered_failure(
            serial_log,
            "unexpected milestone occurrence count: FirmwareReady expected 1, got 2",
        )

    def test_registered_milestones_reject_a_replayed_pre_booti_marker(
        self,
    ) -> None:
        marker_exchange = "=> echo ASTERINAS_PRE_BOOTI\nASTERINAS_PRE_BOOTI\n"
        serial_log = self.registered_serial_log().replace(
            marker_exchange,
            marker_exchange * 2,
            1,
        )

        self.assert_registered_failure(
            serial_log,
            "unexpected milestone occurrence count: ArtifactsLoaded expected 2, got 4",
        )

    def test_registered_milestones_reject_an_early_duplicate(self) -> None:
        profile = self.registered_profile()
        kernel_ready = next(
            milestone.line.decode()
            for milestone in profile.validation.milestones
            if milestone.stage.value == "KernelReady"
        )
        rootfs_ready = next(
            milestone.line.decode()
            for milestone in profile.validation.milestones
            if milestone.stage.value == "RootfsReady"
        )
        serial_log = self.registered_serial_log().replace(
            kernel_ready,
            f"{rootfs_ready}\n{kernel_ready}",
            1,
        )

        self.assert_registered_failure(
            serial_log,
            "unexpected milestone occurrence count: RootfsReady expected 1, got 2",
        )

    def test_rejects_the_wrong_ad_extension_for_the_selected_profile(self) -> None:
        profile = qemu_uboot_booti.profile_by_name("megrez-sv48-svadu-fast")
        serial_log = CLEAN_SERIAL_LOG.replace(
            "console=ttyS0 loglevel=info init=/init",
            profile.bootargs,
        )

        audit = qemu_uboot_booti.audit_serial_log(
            serial_log,
            marker_event=self.successful_marker_event(),
            profile=profile,
        )

        self.assertFalse(audit.passed)
        self.assertIn("OpenSBI did not prove forced svadu mode", audit.failures)

    def assert_rejected(self, serial_log: str) -> None:
        audit = qemu_uboot_booti.audit_serial_log(
            serial_log,
            marker_event=self.successful_marker_event(),
        )
        self.assertFalse(audit.passed)

    def test_rejects_a_kernel_crc_mismatch(self) -> None:
        self.assert_rejected(CLEAN_SERIAL_LOG.replace("57c40418", "deadbeef"))

    def test_rejects_a_missing_console_bootarg(self) -> None:
        self.assert_rejected(CLEAN_SERIAL_LOG.replace("console=ttyS0 ", ""))

    def test_rejects_bootargs_from_the_persistent_environment(self) -> None:
        self.assert_rejected(
            CLEAN_SERIAL_LOG.replace(
                "bootargs=console=ttyS0 loglevel=info init=/init",
                "bootargs=cpu_no_boost_1_6ghz",
            )
        )

    def test_rejects_a_missing_fdt_bootargs_print(self) -> None:
        self.assert_rejected(CLEAN_SERIAL_LOG.replace("=> fdt print /chosen\n", ""))

    def test_rejects_a_mismatched_dtb_bootargs_proof(self) -> None:
        self.assert_rejected(
            CLEAN_SERIAL_LOG.replace(
                'bootargs = "console=ttyS0 loglevel=info init=/init";',
                'bootargs = "cpu_no_boost_1_6ghz";',
            )
        )

    def test_rejects_bootargs_transaction_after_userspace_marker(self) -> None:
        transaction = """\
=> setenv bootargs "console=ttyS0 loglevel=info init=/init"
=> printenv bootargs
bootargs=console=ttyS0 loglevel=info init=/init
=> fdt set /chosen bootargs "console=ttyS0 loglevel=info init=/init"
=> fdt print /chosen
    bootargs = "console=ttyS0 loglevel=info init=/init";
"""
        serial_log = CLEAN_SERIAL_LOG.replace(transaction, "") + transaction

        self.assert_rejected(serial_log)

    def test_rejects_a_persistent_environment_write(self) -> None:
        self.assert_rejected(CLEAN_SERIAL_LOG + "=> saveenv\nSaving Environment\n")

    def test_does_not_treat_a_longer_command_as_saveenv(self) -> None:
        audit = qemu_uboot_booti.audit_serial_log(
            CLEAN_SERIAL_LOG + "=> saveenvironment\nUnknown command\n",
            marker_event=self.successful_marker_event(),
        )

        self.assertTrue(audit.passed, audit.failures)

    def test_rejects_a_panic_even_after_the_userspace_marker(self) -> None:
        self.assert_rejected(CLEAN_SERIAL_LOG + "Uncaught panic:\n")

    def test_rejects_svadu_when_the_run_requires_svade(self) -> None:
        self.assert_rejected(CLEAN_SERIAL_LOG.replace("svade", "svadu"))

    def test_rejects_a_missing_pre_booti_marker(self) -> None:
        self.assert_rejected(CLEAN_SERIAL_LOG.replace("ASTERINAS_PRE_BOOTI\n", "", 1))

    def test_rejects_a_missing_kernel_entry_marker(self) -> None:
        self.assert_rejected(CLEAN_SERIAL_LOG.replace("Enter riscv_boot\n", ""))

    def test_rejects_a_payload_that_overlaps_a_bdinfo_reserved_range(self) -> None:
        self.assert_rejected(
            CLEAN_SERIAL_LOG.replace(
                "[0x80000000-0x8004ffff]",
                "[0x80200000-0x80200fff]",
            )
        )

    def test_rejects_crcs_reported_for_the_wrong_artifact_addresses(self) -> None:
        swapped = CLEAN_SERIAL_LOG.replace("crc32 for 80200000", "crc32 for dead0000")
        swapped = swapped.replace("crc32 for 83000000", "crc32 for 80200000")
        swapped = swapped.replace("crc32 for dead0000", "crc32 for 83000000")

        self.assert_rejected(swapped)


if __name__ == "__main__":
    unittest.main()
