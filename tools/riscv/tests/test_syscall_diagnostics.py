#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Run the kernel's pure syscall diagnostic state with the host Rust toolchain."""

from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
STATE = ROOT / "kernel/src/syscall/diagnostics/state.rs"

RUST_TESTS = r"""
use state::{COMPLETION_HISTORY_LEN, ExitObservation, LifecycleBudget, LifecycleEvent, Outcome, State, Snapshot, snapshot_slice};

#[test]
fn exit_observations_report_final_normal_and_signal_status() {
    let normal = ExitObservation {
        pid: 101, tid: 102, ppid: 99,
        termination_status: 7 << 8,
        signal: None,
        finished_jiffies: 123,
    };
    assert_eq!(normal.to_string(), "syscall_diag lifecycle=normal_exit pid=101 tid=102 ppid=99 termination_status=1792 finished_jiffies=123");
    let signal = ExitObservation {
        termination_status: 9,
        signal: Some(9),
        ..normal
    };
    assert_eq!(signal.to_string(), "syscall_diag lifecycle=signal_exit pid=101 tid=102 ppid=99 signal=9 termination_status=9 finished_jiffies=123");
}

#[test]
fn pending_call_is_visible_before_completion() {
    let mut state = State::default();
    state.enter(98, [1, 2, 3, 4, 5, 6], 42);
    let call = state.current.unwrap();
    assert_eq!((call.sequence, call.number, call.entered_jiffies), (1, 98, 42));
    assert_eq!(call.args, [1, 2, 3, 4, 5, 6]);
    assert!(state.completed.is_none());
}

#[test]
fn completions_clear_current_and_keep_only_the_latest() {
    let mut state = State::default();
    for (index, outcome) in [Outcome::Return(7), Outcome::Error(-10), Outcome::NoReturn]
        .into_iter().enumerate() {
        state.enter(260, [0; 6], 50);
        let done = state.complete(outcome, 52).unwrap();
        assert_eq!(done.call.sequence, index as u64 + 1);
        assert_eq!(done.outcome, outcome);
        assert_eq!(done.finished_jiffies, 52);
        assert_eq!(state.completed, Some(done));
        assert!(state.current.is_none());
        assert!(state.complete(outcome, 53).is_none());
    }
}

#[test]
fn completion_history_is_bounded_and_ordered_oldest_first() {
    let mut state = State::default();
    for sequence in 1..=(COMPLETION_HISTORY_LEN as u64 + 2) {
        state.enter(200 + sequence, [sequence; 6], sequence * 2);
        state.complete(Outcome::Return(sequence as isize), sequence * 2 + 1);
    }
    let sequences: Vec<_> = state.completed_history()
        .map(|completion| completion.call.sequence)
        .collect();
    assert_eq!(sequences, (3..=COMPLETION_HISTORY_LEN as u64 + 2).collect::<Vec<_>>());
    state.clear_calls();
    assert_eq!(state.completed_history().count(), 0);
}

#[test]
fn clearing_an_exec_generation_preserves_sequence() {
    let mut state = State::default();
    state.enter(221, [9; 6], 0);
    state.complete(Outcome::NoReturn, 1);
    state.clear_calls();
    assert!(state.current.is_none());
    assert!(state.completed.is_none());
    state.enter(64, [0; 6], 2);
    assert_eq!(state.current.unwrap().sequence, 2);
}

#[test]
fn idle_and_disabled_snapshots_are_explicit() {
    let idle = State::default();
    assert_eq!(Snapshot::new(false, 11, 12, 99, idle).to_string(),
        "{\"version\":2,\"enabled\":false,\"pid\":11,\"tid\":12,\"snapshot_jiffies\":99,\"current\":null,\"completed\":null,\"history\":[]}\n");
    let mut pending = idle;
    pending.enter(98, [1; 6], 90);
    let disabled = Snapshot::new(false, 11, 12, 99, pending).to_string();
    assert!(disabled.ends_with("\"current\":null,\"completed\":null,\"history\":[]}\n"));
    assert!(Snapshot::new(true, 11, 12, 99, idle).to_string().contains("\"enabled\":true"));
}

#[test]
fn json_preserves_scalar_registers_and_distinguishes_outcomes() {
    let mut state = State::default();
    state.enter(98, [u64::MAX, 2, 3, 4, 5, 6], 42);
    state.complete(Outcome::Error(-10), 43);
    state.enter(64, [0; 6], 44);
    let json = Snapshot::new(true, 11, 12, 45, state).to_string();
    assert_eq!(json, "{\"version\":2,\"enabled\":true,\"pid\":11,\"tid\":12,\"snapshot_jiffies\":45,\"current\":{\"sequence\":2,\"number\":64,\"args\":[0,0,0,0,0,0],\"entered_jiffies\":44},\"completed\":{\"sequence\":1,\"number\":98,\"args\":[18446744073709551615,2,3,4,5,6],\"entered_jiffies\":42,\"finished_jiffies\":43,\"result\":-10,\"outcome\":\"error\"},\"history\":[{\"sequence\":1,\"number\":98,\"args\":[18446744073709551615,2,3,4,5,6],\"entered_jiffies\":42,\"finished_jiffies\":43,\"result\":-10,\"outcome\":\"error\"}]}\n");
    state.complete(Outcome::NoReturn, 46);
    assert!(Snapshot::new(true, 11, 12, 47, state).to_string().contains("\"result\":null,\"outcome\":\"no_return\""));
    state.enter(64, [0; 6], 48);
    state.complete(Outcome::Return(7), 49);
    assert!(Snapshot::new(true, 11, 12, 50, state).to_string().contains("\"result\":7,\"outcome\":\"return\""));
}

#[test]
fn chunked_reads_seeks_and_eof_use_the_same_snapshot() {
    let bytes = b"abcdef";
    assert_eq!(snapshot_slice(bytes, 0, 2), b"ab");
    assert_eq!(snapshot_slice(bytes, 2, usize::MAX), b"cdef");
    assert_eq!(snapshot_slice(bytes, 1, 0), b"");
    assert_eq!(snapshot_slice(bytes, 6, 99), b"");
    assert_eq!(snapshot_slice(bytes, usize::MAX, usize::MAX), b"");
    assert_eq!(snapshot_slice(bytes, 0, 2), b"ab");
}

#[test]
fn lifecycle_budget_has_finite_output_and_explicit_suppression() {
    let budget = LifecycleBudget::new();
    for _ in 0..1024 {
        assert_eq!(budget.next(), LifecycleEvent::Record);
    }
    assert_eq!(budget.next(), LifecycleEvent::Suppressed(1));
    assert_eq!(budget.next(), LifecycleEvent::Suppressed(2));
    assert_eq!(budget.next(), LifecycleEvent::Quiet);
    assert_eq!(budget.next(), LifecycleEvent::Suppressed(4));
}
"""


class SyscallDiagnosticsTests(unittest.TestCase):
    def test_proc_and_entry_integration_exist(self) -> None:
        diagnostics = ROOT / "kernel/src/syscall/diagnostics.rs"
        self.assertTrue(
            diagnostics.is_file(), "missing default-off diagnostic integration"
        )
        source = diagnostics.read_text()
        self.assertIn('define_flag_param!("asterinas.syscall_diag", ENABLED)', source)
        dispatch = (ROOT / "kernel/src/syscall/mod.rs").read_text()
        self.assertLess(
            dispatch.index("diagnostics::enter("),
            dispatch.index("match seccomp::check("),
        )
        proc = ROOT / "kernel/src/fs/fs_impls/procfs/pid/task/asterinas_syscall.rs"
        self.assertTrue(proc.is_file(), "missing per-open proc snapshot interface")

    def test_actual_kernel_state(self) -> None:
        self.assertTrue(STATE.is_file(), "missing per-thread syscall diagnostic state")
        self.assertTrue(
            "struct ExitObservation" in STATE.read_text(),
            "missing normal-exit lifecycle formatter",
        )
        with tempfile.TemporaryDirectory(prefix="asterinas-syscall-diag-") as directory:
            directory = Path(directory)
            harness = directory / "tests.rs"
            harness.write_text(f'#[path = "{STATE}"]\nmod state;\n' + RUST_TESTS)
            binary = directory / "tests"
            subprocess.run(
                ["rustc", "--edition=2024", "--test", str(harness), "-o", str(binary)],
                check=True,
            )
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
