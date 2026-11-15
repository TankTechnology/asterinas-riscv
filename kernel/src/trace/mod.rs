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
pub mod kallsyms;
pub mod ring;

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
