#!/usr/bin/env python3

from __future__ import annotations

import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DIAGNOSTIC_SOURCE = REPOSITORY_ROOT / "kernel/src/first_process_diag.rs"
INIT_SOURCE = REPOSITORY_ROOT / "kernel/src/init.rs"
LIB_SOURCE = REPOSITORY_ROOT / "kernel/src/lib.rs"
TASK_SOURCE = REPOSITORY_ROOT / "kernel/src/thread/task.rs"


def _ordered(source: str, needles: tuple[str, ...]) -> bool:
    positions = [source.find(needle) for needle in needles]
    return all(position >= 0 for position in positions) and positions == sorted(positions)


class FirstProcessDiagnosticSourceTests(unittest.TestCase):
    def test_diagnostic_is_opt_in_bounded_and_uses_early_console(self) -> None:
        self.assertTrue(DIAGNOSTIC_SOURCE.is_file())
        source = DIAGNOSTIC_SOURCE.read_text()

        self.assertIn(
            'define_flag_param!("asterinas.first_process_diag", REQUESTED)', source
        )
        self.assertIn('ostd::early_println!("{}", marker)', source)
        for stage in (
            "diagnostic_active",
            "process_components_ready",
            "device_init_ready",
            "stdio_init_ready",
            "user_enter",
            "user_first_return",
            "user_first_syscall",
            "user_first_write_returned",
        ):
            self.assertIn(f"stage={stage}", source)
        self.assertIn("if self.user_enter_seen", source)
        self.assertIn("if self.first_return_seen", source)
        self.assertIn("if self.first_syscall_seen", source)
        self.assertIn("if self.first_write_returned", source)

    def test_riscv_module_and_startup_hooks_are_ordered(self) -> None:
        lib = LIB_SOURCE.read_text()
        self.assertIn(
            '#[cfg(target_arch = "riscv64")]\nmod first_process_diag;', lib
        )

        startup = INIT_SOURCE.read_text().split(
            "pub(super) fn on_first_process_startup", maxsplit=1
        )[1]
        self.assertTrue(
            _ordered(
                startup,
                (
                    "component::init_all(InitStage::Process",
                    "first_process_diag::on_process_components_ready()",
                    "device::init_in_first_process(ctx)",
                    "first_process_diag::on_device_init_ready()",
                    "fs::init_in_first_process(ctx)",
                    "first_process_diag::on_stdio_init_ready()",
                ),
            )
        )

    def test_pid1_hooks_straddle_only_the_observed_operations(self) -> None:
        task = TASK_SOURCE.read_text()
        self.assertTrue(
            _ordered(
                task,
                (
                    "crate::init::on_first_process_startup(&ctx)",
                    "FirstProcessDiagnostics::new_if_active()",
                    "diagnostics.on_user_enter(user_mode.context())",
                    "user_mode.execute(has_kernel_event_fn)",
                ),
            )
        )
        self.assertTrue(
            _ordered(
                task,
                (
                    "diagnostics.on_user_syscall_trap(user_ctx)",
                    "diagnostics.on_syscall_enter(user_ctx)",
                    "handle_syscall(&ctx, user_ctx)",
                    "diagnostics.on_syscall_return(user_ctx)",
                ),
            )
        )


if __name__ == "__main__":
    unittest.main()
