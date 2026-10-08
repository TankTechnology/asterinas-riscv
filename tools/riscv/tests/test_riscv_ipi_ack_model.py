# SPDX-License-Identifier: MPL-2.0
"""Bounded interleaving check for RISC-V IPI acknowledgement ordering.

The browser/page-fault stall recorded on 2026-09-16 leaves a remote TLB ACK
false while every other CPU waits for the same page-table lock. This model
checks the lost-wakeup window between draining the callback queue and clearing
SSIP. It does not model SBI implementation details or prove the whole kernel.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class State:
    queued: int = 1
    pending: bool = True
    sent: int = 1
    handled: int = 0


def apply(state: State, event: str) -> State:
    if event == "enqueue":
        return State(state.queued + 1, state.pending, state.sent + 1, state.handled)
    if event == "send_ipi":
        return State(state.queued, True, state.sent, state.handled)
    if event == "clear_ssip":
        return State(state.queued, False, state.sent, state.handled)
    if event == "drain_callbacks":
        return State(0, state.pending, state.sent, state.handled + state.queued)
    raise ValueError(event)


def terminal_states(ack_before_callbacks: bool) -> set[State]:
    handler = (
        ("clear_ssip", "drain_callbacks")
        if ack_before_callbacks
        else ("drain_callbacks", "clear_ssip")
    )
    sender = ("enqueue", "send_ipi")
    results: set[State] = set()

    def explore(state: State, handler_index: int, sender_index: int) -> None:
        if handler_index == len(handler) and sender_index == len(sender):
            results.add(state)
            return
        if handler_index < len(handler):
            explore(apply(state, handler[handler_index]), handler_index + 1, sender_index)
        if sender_index < len(sender):
            explore(apply(state, sender[sender_index]), handler_index, sender_index + 1)

    explore(State(), 0, 0)
    return results


class RiscvIpiAckModelTest(unittest.TestCase):
    def test_post_callback_ack_can_strand_remote_tlb_request(self) -> None:
        stranded = {
            state
            for state in terminal_states(False)
            if state.queued > 0 and not state.pending
        }
        self.assertTrue(stranded)

    def test_pre_callback_ack_cannot_strand_callback_in_bounded_model(self) -> None:
        stranded = {
            state
            for state in terminal_states(True)
            if state.queued > 0 and not state.pending
        }
        self.assertFalse(stranded)

    def test_riscv_soft_interrupt_is_cleared_before_callbacks(self) -> None:
        source = (REPO_ROOT / "ostd/src/arch/riscv/trap/mod.rs").read_text()
        branch = source.split("Interrupt::SupervisorSoft => {", 1)[1].split("\n        }", 1)[0]
        self.assertLess(
            branch.index("riscv::register::sip::clear_ssoft()"),
            branch.index("call_irq_callback_functions("),
        )

    def test_generic_post_callback_ack_does_not_clear_software_ipi(self) -> None:
        source = (REPO_ROOT / "ostd/src/arch/riscv/irq/mod.rs").read_text()
        self.assertIn("InterruptSource::Software => {}", source)


if __name__ == "__main__":
    unittest.main()
