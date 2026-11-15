// SPDX-License-Identifier: MPL-2.0

//! Kernel tracing support.
//!
//! The pieces:
//!
//! * [`kallsyms`] -- address to function name, filled in after linking.
//! * [`ring`] -- the per-CPU record ring.
//! * [`event`] -- the event registry and the enabled switches.
//! * the files under `/proc/trace` (in `fs::fs_impls::procfs::trace`) -- the
//!   reader and the controls.
//!
//! Emitters use the [`trace_event!`] macro:
//!
//! ```rust,ignore
//! trace_event!(events::syscall::SYS_ENTER, pid, tid, "nr={}", number);
//! ```
//!
//! The macro checks the event's switch first, so a disabled event costs one
//! relaxed load and a branch -- nothing is formatted and no lock is taken.

pub mod event;
pub mod events;
#[cfg(target_arch = "riscv64")]
pub mod function;
pub mod kallsyms;
pub mod profile;
pub mod ring;

/// The function tracer is RISC-V assembly; elsewhere it is inert, so the
/// `/proc/trace` switches still exist and simply do nothing.
#[cfg(not(target_arch = "riscv64"))]
pub mod function {
    pub fn init() {}

    pub fn sync_armed() {}
}

/// Emits a trace record for `$event`, if it is enabled.
///
/// The arguments after the process and thread ids are formatted into the
/// record's payload; they are only evaluated when the event is on.
#[macro_export]
macro_rules! trace_event {
    ($event:expr, $pid:expr, $tid:expr, $($arg:tt)*) => {{
        let event = $event;
        if event.enabled() {
            event.trace($pid, $tid, ::core::format_args!($($arg)*));
        }
    }};
}

/// Installs the hooks that have to live inside other subsystems.
///
/// The interrupt hook is the only one of those; the scheduler, page-fault and
/// network hooks are emitted at their call sites in the kernel.
pub fn init() {
    function::init();
    profile::init();
    ostd::irq::inject_irq_hook(trace_irq_hook);
    aster_block::bio::inject_bio_hook(events::block::trace_hook);
}

/// Runs around every interrupt handler invocation.
fn trace_irq_hook(irq_num: u8, entering: bool) {
    use events::irq::{IRQ_HANDLER_ENTRY, IRQ_HANDLER_EXIT};

    let event: &event::TraceEvent = if entering {
        &IRQ_HANDLER_ENTRY
    } else {
        &IRQ_HANDLER_EXIT
    };
    if !event.enabled() {
        return;
    }
    let (pid, tid) = ostd::task::Task::current()
        .map(|task| crate::thread::task_ids(&task))
        .unwrap_or((0, 0));
    event.trace(pid, tid, format_args!("irq={}", irq_num));
}
