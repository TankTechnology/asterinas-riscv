// SPDX-License-Identifier: MPL-2.0

//! Attaching eBPF programs to trace events.
//!
//! This is the kernel's own attach point: no kprobes and no instruction
//! patching. When an event fires, the program attached to it runs with a small
//! context (timestamp, pid, tid, event id, cpu) and the helpers it may call.
//!
//! An attached event counts as enabled even when its `/proc/trace` switch is
//! off, so attaching does not require also turning the event's ring records
//! on; conversely a program alone does not write records unless the event's
//! own switch is on.

use core::sync::atomic::{AtomicU32, Ordering};

use ostd::{cpu::PinCurrentCpu, sync::LocalIrqDisabled, util::id_set::Id};

use super::{TraceCtx, prog::Program};
use crate::{
    events::IoEvents,
    fs::{
        file::{AccessMode, FileCommon, FileLike, StatusFlags},
        pseudofs::AnonInodeFs,
    },
    prelude::*,
    process::signal::{PollHandle, Pollable, Pollee},
};

/// Event ids this table has room for.
const MAX_EVENTS: usize = 32;

static ATTACHED: [SpinLock<Option<Arc<Program>>, LocalIrqDisabled>; MAX_EVENTS] =
    [const { SpinLock::new(None) }; MAX_EVENTS];

/// Bit `id` is set while event `id` has a program.
static MASK: AtomicU32 = AtomicU32::new(0);

fn slot(event_id: u16) -> Result<usize> {
    let slot = event_id as usize;
    if slot >= MAX_EVENTS {
        return_errno_with_message!(Errno::EINVAL, "the event id is out of range");
    }
    Ok(slot)
}

/// Attaches `program` to `event_id`, detaching whatever was there.
pub fn attach(event_id: u16, program: Arc<Program>) -> Result<()> {
    let slot = slot(event_id)?;
    let previous = ATTACHED[slot].lock().replace(program.clone());
    if let Some(previous) = previous {
        previous.set_attached_event(None);
    }
    program.set_attached_event(Some(event_id));
    MASK.fetch_or(1 << slot, Ordering::Relaxed);
    Ok(())
}

pub fn detach(event_id: u16) -> Result<()> {
    let slot = slot(event_id)?;
    MASK.fetch_and(!(1 << slot), Ordering::Relaxed);
    if let Some(program) = ATTACHED[slot].lock().take() {
        program.set_attached_event(None);
    }
    Ok(())
}

/// The cheap check the emitters make: one atomic load.
pub fn is_attached(event_id: u16) -> bool {
    let slot = event_id as usize;
    slot < MAX_EVENTS && MASK.load(Ordering::Relaxed) & (1 << slot) != 0
}

/// The program attached to `event_id`, if any.
pub fn attached(event_id: u16) -> Option<Arc<Program>> {
    let slot = slot(event_id).ok()?;
    let program = ATTACHED[slot].lock().clone();
    program
}

/// Runs the program attached to `event_id`, if there is one.
///
/// Called from the emitting context, which may be an interrupt handler.
pub fn run(event_id: u16, pid: u32, tid: u32, args: [u64; 3]) {
    run_at(
        event_id,
        pid,
        tid,
        args,
        aster_time::read_monotonic_time().as_nanos() as u64,
    );
}

/// Like [`run`], but with the emitter supplying the timestamp.
///
/// An emitter that can run inside the clocksource's own update -- the sampling
/// profiler and the function tracer -- must not read the monotonic clock, so it
/// passes a lock-free timestamp instead.
pub fn run_at(event_id: u16, pid: u32, tid: u32, args: [u64; 3], timestamp_ns: u64) {
    let Some(program) = attached(event_id) else {
        return;
    };

    let cpu = {
        let guard = ostd::task::disable_preempt();
        guard.current_cpu().as_usize() as u32
    };

    let ctx = TraceCtx {
        timestamp_ns,
        pid,
        tid,
        event: event_id as u32,
        cpu,
        args,
    };
    program.run(&ctx);
}

/// Renders the attachment table for `/proc/trace/bpf`.
pub fn render(writer: &mut dyn core::fmt::Write) -> core::fmt::Result {
    writeln!(writer, "# programs attached to trace events")?;
    let mut any = false;
    for (event_id, slot) in ATTACHED.iter().enumerate() {
        let program = slot.lock().clone();
        let Some(program) = program else {
            continue;
        };
        any = true;
        let event = crate::trace::event::event_by_id(event_id as u16);
        let name = event
            .map(|event| alloc::format!("{}:{}", event.subsystem(), event.name()))
            .unwrap_or_else(|| alloc::format!("event {}", event_id));
        writeln!(
            writer,
            "{} program \"{}\" runs={} errors={} steps={}",
            name,
            program.name(),
            program.runs(),
            program.run_errors(),
            program.steps(),
        )?;
        for (index, map) in program.maps().iter().enumerate() {
            writeln!(
                writer,
                "  map[{}] \"{}\" type={} key={} value={} entries={} lookups={} updates={}",
                index,
                map.name(),
                map.map_type(),
                map.key_size(),
                map.value_size(),
                map.max_entries(),
                map.lookups(),
                map.updates(),
            )?;
        }
    }
    if !any {
        writeln!(writer, "no program is attached")?;
    }
    Ok(())
}

/// The file object a successful `BPF_RAW_TRACEPOINT_OPEN` returns. Closing it
/// detaches the program.
pub struct LinkFile {
    event_id: u16,
    common: FileCommon,
    pollee: Pollee,
}

impl LinkFile {
    pub fn new(event_id: u16) -> Self {
        Self {
            event_id,
            common: FileCommon::new(
                AnonInodeFs::new_path(|_| "anon_inode:[bpf-link]".to_string()),
                StatusFlags::empty(),
            ),
            pollee: Pollee::new(),
        }
    }

    #[allow(dead_code)]
    pub fn event_id(&self) -> u16 {
        self.event_id
    }
}

impl Drop for LinkFile {
    fn drop(&mut self) {
        let _ = detach(self.event_id);
    }
}

impl Pollable for LinkFile {
    fn poll(&self, mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        self.pollee.poll_with(mask, None, IoEvents::empty)
    }
}

impl FileLike for LinkFile {

    fn common(&self) -> &FileCommon {
        &self.common
    }

    fn dump_proc_fdinfo(
        self: Arc<Self>,
        fd_flags: crate::fs::file::file_table::FdFlags,
    ) -> Box<dyn core::fmt::Display> {
        super::fdinfo(&self.common, self.access_mode(), fd_flags, "link")
    }

    fn is_scm_rights_proven_leaf(&self) -> bool {
        true
    }
}
