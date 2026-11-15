// SPDX-License-Identifier: MPL-2.0

//! `block` events: block I/O requests.
//!
//! Both are emitted from the BIO hook in `aster-block`, which is the one place
//! every read and write goes through -- the filesystems and the page cache
//! submit `Bio`s, the device drivers complete them.

use aster_block::bio::BioHookEvent;

use crate::trace::event::TraceEvent;

/// The subsystem name as it appears under `events/`.
pub const NAME: &str = "block";

/// Emitted when a BIO is handed to the device.
pub static BLOCK_RQ_ISSUE: TraceEvent = TraceEvent::new(
    10,
    NAME,
    "block_rq_issue",
    "field:sector long; field:nr_sectors long; field:type int",
);

/// Emitted when a BIO completes, with its status.
pub static BLOCK_RQ_COMPLETE: TraceEvent = TraceEvent::new(
    11,
    NAME,
    "block_rq_complete",
    "field:sector long; field:nr_sectors long; field:status int",
);

/// The hook `aster-block` calls on submission and completion.
pub fn trace_hook(event: &BioHookEvent<'_>) {
    let trace_event = if event.submitted {
        &BLOCK_RQ_ISSUE
    } else {
        &BLOCK_RQ_COMPLETE
    };
    if !trace_event.enabled() {
        return;
    }
    let sector = event.sid_range.start.to_raw();
    let sectors = event.sid_range.end.to_raw().saturating_sub(sector);
    let (pid, tid) = ostd::task::Task::current()
        .map(|task| crate::thread::task_ids(&task))
        .unwrap_or((0, 0));
    let kind = if event.submitted {
        event.type_ as u64
    } else {
        event.status as u64
    };
    trace_event.trace(
        pid,
        tid,
        [sectors as u64, kind, sector as u64],
        format_args!(
            "sector={} nr_sectors={} kind={}",
            sector,
            sectors,
            if event.submitted {
                event.type_ as u8
            } else {
                event.status as u8
            }
        ),
    );
}
