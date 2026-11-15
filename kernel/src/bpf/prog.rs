// SPDX-License-Identifier: MPL-2.0

//! A loaded eBPF program: its instructions, the maps it references, and the
//! counters a user asks about through `BPF_OBJ_GET_INFO_BY_FD`.

use core::sync::atomic::{AtomicU64, Ordering};

use ostd::sync::LocalIrqDisabled;

use super::{TraceCtx, insn::Insn, interp, map::Map};
use crate::{
    events::IoEvents,
    fs::{
        file::{AccessMode, FileCommon, FileLike, StatusFlags},
        pseudofs::AnonInodeFs,
    },
    prelude::*,
    process::signal::{PollHandle, Pollable, Pollee},
};

/// `BPF_PROG_TYPE_SOCKET_FILTER`, the only type this kernel accepts today.
pub const BPF_PROG_TYPE_SOCKET_FILTER: u32 = 1;
/// A type of our own: run by a trace event.
pub const BPF_PROG_TYPE_TRACE_EVENT: u32 = 0x8000_0001;

pub struct Program {
    pub(super) insns: Vec<Insn>,
    pub(super) maps: Vec<Arc<Map>>,
    prog_type: u32,
    name: String,
    #[allow(dead_code)] // shown by /proc/trace/bpf once the tool asks for it
    license: String,
    attached_event: SpinLock<Option<u16>, LocalIrqDisabled>,
    runs: AtomicU64,
    run_errors: AtomicU64,
    steps: AtomicU64,
}

impl Program {
    pub fn new(
        insns: Vec<Insn>,
        maps: Vec<Arc<Map>>,
        prog_type: u32,
        name: &str,
        license: &str,
    ) -> Arc<Self> {
        Arc::new(Self {
            insns,
            maps,
            prog_type,
            name: name.to_string(),
            license: license.to_string(),
            attached_event: SpinLock::new(None),
            runs: AtomicU64::new(0),
            run_errors: AtomicU64::new(0),
            steps: AtomicU64::new(0),
        })
    }

    pub fn insns(&self) -> &[Insn] {
        &self.insns
    }

    pub fn maps(&self) -> &[Arc<Map>] {
        &self.maps
    }

    pub fn prog_type(&self) -> u32 {
        self.prog_type
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    #[allow(dead_code)]
    pub fn license(&self) -> &str {
        &self.license
    }

    pub fn runs(&self) -> u64 {
        self.runs.load(Ordering::Relaxed)
    }

    pub fn run_errors(&self) -> u64 {
        self.run_errors.load(Ordering::Relaxed)
    }

    pub fn steps(&self) -> u64 {
        self.steps.load(Ordering::Relaxed)
    }

    #[allow(dead_code)]
    pub fn attached_event(&self) -> Option<u16> {
        *self.attached_event.lock()
    }

    pub fn set_attached_event(&self, event: Option<u16>) {
        *self.attached_event.lock() = event;
    }

    /// Runs the program on `ctx`.
    ///
    /// This is called from the context that emitted the trace event, which may
    /// be an interrupt handler or the scheduler. The interpreter never
    /// allocates, its maps take IRQ-safe locks, and the step count is bounded.
    pub fn run(&self, ctx: &TraceCtx) -> u64 {
        self.runs.fetch_add(1, Ordering::Relaxed);
        match interp::run(self, ctx) {
            Ok((retval, steps)) => {
                self.steps.fetch_add(steps, Ordering::Relaxed);
                retval
            }
            Err(error) => {
                self.run_errors.fetch_add(1, Ordering::Relaxed);
                debug!("a bpf program faulted: {:?}", error);
                u64::MAX
            }
        }
    }
}

/// The file object behind a program file descriptor.
pub struct ProgFile {
    program: Arc<Program>,
    common: FileCommon,
    pollee: Pollee,
}

impl ProgFile {
    pub fn new(program: Arc<Program>) -> Self {
        Self {
            program,
            common: FileCommon::new(
                AnonInodeFs::new_path(|_| "anon_inode:[bpf-prog]".to_string()),
                StatusFlags::empty(),
            ),
            pollee: Pollee::new(),
        }
    }

    pub fn program(&self) -> &Arc<Program> {
        &self.program
    }
}

impl Pollable for ProgFile {
    fn poll(&self, mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        self.pollee.poll_with(mask, None, IoEvents::empty)
    }
}

impl FileLike for ProgFile {
    fn access_mode(&self) -> AccessMode {
        AccessMode::O_RDWR
    }

    fn common(&self) -> &FileCommon {
        &self.common
    }

    fn dump_proc_fdinfo(
        self: Arc<Self>,
        fd_flags: crate::fs::file::file_table::FdFlags,
    ) -> Box<dyn core::fmt::Display> {
        super::fdinfo(&self.common, self.access_mode(), fd_flags, "prog")
    }

    fn is_scm_rights_proven_leaf(&self) -> bool {
        true
    }
}
