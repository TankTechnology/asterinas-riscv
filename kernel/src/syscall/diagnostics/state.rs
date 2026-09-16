// SPDX-License-Identifier: MPL-2.0

//! Fixed-size syscall records and their versioned, scalar-only representation.

use core::{
    fmt,
    sync::atomic::{AtomicU64, Ordering},
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct Call {
    pub sequence: u64,
    pub number: u64,
    pub args: [u64; 6],
    pub entered_jiffies: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Outcome {
    Return(isize),
    Error(isize),
    NoReturn,
}

impl Outcome {
    pub fn label(self) -> &'static str {
        match self {
            Self::Return(_) => "return",
            Self::Error(_) => "error",
            Self::NoReturn => "no_return",
        }
    }

    pub fn result(self) -> Option<isize> {
        match self {
            Self::Return(result) | Self::Error(result) => Some(result),
            Self::NoReturn => None,
        }
    }

    /// Formats the scalar result without allocating, including JSON null.
    pub fn result_display(self) -> impl fmt::Display {
        struct ResultDisplay(Option<isize>);

        impl fmt::Display for ResultDisplay {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                match self.0 {
                    Some(result) => write!(f, "{}", result),
                    None => f.write_str("null"),
                }
            }
        }

        ResultDisplay(self.result())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct Completion {
    pub call: Call,
    pub finished_jiffies: u64,
    pub outcome: Outcome,
}

pub(crate) const COMPLETION_HISTORY_LEN: usize = 32;

#[derive(Clone, Copy, Debug, Default)]
pub(crate) struct State {
    sequence: u64,
    pub current: Option<Call>,
    pub completed: Option<Completion>,
    history: [Option<Completion>; COMPLETION_HISTORY_LEN],
    history_next: usize,
    history_count: usize,
}

impl State {
    pub fn enter(&mut self, number: u64, args: [u64; 6], entered_jiffies: u64) {
        // Stop assigning records if the sequence space is ever exhausted,
        // rather than aliasing a previous call's sequence number.
        let Some(sequence) = self.sequence.checked_add(1) else {
            self.current = None;
            return;
        };
        self.sequence = sequence;
        self.current = Some(Call {
            sequence,
            number,
            args,
            entered_jiffies,
        });
    }

    pub fn complete(&mut self, outcome: Outcome, finished_jiffies: u64) -> Option<Completion> {
        let completion = Completion {
            call: self.current.take()?,
            finished_jiffies,
            outcome,
        };
        self.completed = Some(completion);
        self.history[self.history_next] = Some(completion);
        self.history_next = (self.history_next + 1) % COMPLETION_HISTORY_LEN;
        self.history_count = (self.history_count + 1).min(COMPLETION_HISTORY_LEN);
        Some(completion)
    }

    pub fn completed_history(&self) -> impl Iterator<Item = Completion> + '_ {
        let first = (self.history_next + COMPLETION_HISTORY_LEN - self.history_count)
            % COMPLETION_HISTORY_LEN;
        (0..self.history_count).map(move |offset| {
            self.history[(first + offset) % COMPLETION_HISTORY_LEN]
                .expect("occupied completion history entry")
        })
    }

    pub fn clear_calls(&mut self) {
        self.current = None;
        self.completed = None;
        self.history = [None; COMPLETION_HISTORY_LEN];
        self.history_next = 0;
        self.history_count = 0;
    }
}

/// A version 2 snapshot, with global process and thread IDs.
pub(crate) struct Snapshot {
    enabled: bool,
    pid: u32,
    tid: u32,
    snapshot_jiffies: u64,
    state: State,
}

impl Snapshot {
    pub fn new(enabled: bool, pid: u32, tid: u32, snapshot_jiffies: u64, state: State) -> Self {
        Self {
            enabled,
            pid,
            tid,
            snapshot_jiffies,
            state: if enabled { state } else { State::default() },
        }
    }
}

impl fmt::Display for Snapshot {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{{\"version\":2,\"enabled\":{},\"pid\":{},\"tid\":{},\"snapshot_jiffies\":{},\"current\":",
            self.enabled, self.pid, self.tid, self.snapshot_jiffies,
        )?;
        if let Some(call) = self.state.current {
            f.write_str("{")?;
            call.fmt_fields(f)?;
            f.write_str("}")?;
        } else {
            f.write_str("null")?;
        }
        f.write_str(",\"completed\":")?;
        if let Some(completion) = self.state.completed {
            f.write_str("{")?;
            completion.fmt_fields(f)?;
            f.write_str("}")?;
        } else {
            f.write_str("null")?;
        }
        f.write_str(",\"history\":[")?;
        for (index, completion) in self.state.completed_history().enumerate() {
            if index != 0 {
                f.write_str(",")?;
            }
            f.write_str("{")?;
            completion.fmt_fields(f)?;
            f.write_str("}")?;
        }
        f.write_str("]")?;
        f.write_str("}\n")
    }
}

impl Call {
    fn fmt_fields(self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "\"sequence\":{},\"number\":{},\"args\":[{},{},{},{},{},{}],\"entered_jiffies\":{}",
            self.sequence,
            self.number,
            self.args[0],
            self.args[1],
            self.args[2],
            self.args[3],
            self.args[4],
            self.args[5],
            self.entered_jiffies,
        )
    }
}

impl Completion {
    fn fmt_fields(self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.call.fmt_fields(f)?;
        write!(
            f,
            ",\"finished_jiffies\":{},\"result\":{}",
            self.finished_jiffies,
            self.outcome.result_display()
        )?;
        write!(f, ",\"outcome\":\"{}\"", self.outcome.label())
    }
}

/// Returns a bounded slice without overflow, including for seeks beyond EOF.
pub(crate) fn snapshot_slice(snapshot: &[u8], offset: usize, max_len: usize) -> &[u8] {
    if offset >= snapshot.len() {
        return &[];
    }
    let copy_len = (snapshot.len() - offset).min(max_len);
    &snapshot[offset..offset + copy_len]
}

/// One completed exit transition, carrying the final wait-style status.
pub(crate) struct ExitObservation {
    pub pid: u32,
    pub tid: u32,
    pub ppid: u32,
    pub termination_status: u32,
    pub signal: Option<u8>,
    pub finished_jiffies: u64,
}

impl fmt::Display for ExitObservation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let kind = if self.signal.is_some() {
            "signal_exit"
        } else {
            "normal_exit"
        };
        write!(
            f,
            "syscall_diag lifecycle={} pid={} tid={} ppid={}",
            kind, self.pid, self.tid, self.ppid
        )?;
        if let Some(signal) = self.signal {
            write!(f, " signal={}", signal)?;
        }
        write!(
            f,
            " termination_status={} finished_jiffies={}",
            self.termination_status, self.finished_jiffies
        )
    }
}

/// The boot-wide budget includes at most 1024 records and 64 suppression summaries.
pub(crate) struct LifecycleBudget(AtomicU64);

#[derive(Debug, Eq, PartialEq)]
pub(crate) enum LifecycleEvent {
    Record,
    Suppressed(u64),
    Quiet,
}

impl LifecycleBudget {
    pub const fn new() -> Self {
        Self(AtomicU64::new(0))
    }

    pub fn next(&self) -> LifecycleEvent {
        const RECORD_LIMIT: u64 = 1024;
        let Ok(previous) = self
            .0
            .try_update(Ordering::Relaxed, Ordering::Relaxed, |count| {
                count.checked_add(1)
            })
        else {
            return LifecycleEvent::Quiet;
        };
        if previous < RECORD_LIMIT {
            return LifecycleEvent::Record;
        }
        let suppressed = previous - RECORD_LIMIT + 1;
        if suppressed.is_power_of_two() {
            LifecycleEvent::Suppressed(suppressed)
        } else {
            LifecycleEvent::Quiet
        }
    }
}
