// SPDX-License-Identifier: MPL-2.0

//! Per-device I/O counters behind `/proc/diskstats` and `/sys/block/*/stat`.
//!
//! The block layer has no device model to hang these on, so the counters live
//! in a registry keyed by device id next to the devices themselves: created
//! when a device registers, dropped when it unregisters.
//!
//! What is counted is what a completed request moved: one increment per `Bio`
//! that finished successfully, and the sectors it covered. Requests still in
//! flight are counted separately, which is the one number that has to be live
//! rather than accumulated.

use alloc::{collections::BTreeMap, sync::Arc};
use core::sync::atomic::{AtomicU64, Ordering};

use spin::Mutex;

use ostd::sync::Mutex as BlockingMutex;

use crate::{
    DeviceId,
    bio::BioType,
    id::Sid,
};

/// One device's counters.
#[derive(Debug, Default)]
pub struct IoStats {
    reads: AtomicU64,
    read_sectors: AtomicU64,
    writes: AtomicU64,
    write_sectors: AtomicU64,
    in_flight: AtomicU64,
}

/// A consistent-enough copy for a reader; each field is read once.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct IoStatsSnapshot {
    pub reads: u64,
    pub read_sectors: u64,
    pub writes: u64,
    pub write_sectors: u64,
    pub in_flight: u64,
}

impl IoStats {
    /// A request entered the queue.
    pub(crate) fn begin(&self) {
        self.in_flight.fetch_add(1, Ordering::Relaxed);
    }

    /// A request finished. Only completed requests are counted in the
    /// per-direction totals, so a failed request shows up as neither read nor
    /// written -- which is what `iostat` expects to see.
    pub(crate) fn finish(&self, type_: BioType, sectors: u64, completed: bool) {
        self.in_flight.fetch_sub(1, Ordering::Relaxed);
        if !completed {
            return;
        }
        match type_ {
            BioType::Read => {
                self.reads.fetch_add(1, Ordering::Relaxed);
                self.read_sectors.fetch_add(sectors, Ordering::Relaxed);
            }
            BioType::Write => {
                self.writes.fetch_add(1, Ordering::Relaxed);
                self.write_sectors.fetch_add(sectors, Ordering::Relaxed);
            }
            // Cache flushes move no sectors; they are counted nowhere.
            BioType::Flush => {}
        }
    }

    pub fn snapshot(&self) -> IoStatsSnapshot {
        IoStatsSnapshot {
            reads: self.reads.load(Ordering::Relaxed),
            read_sectors: self.read_sectors.load(Ordering::Relaxed),
            writes: self.writes.load(Ordering::Relaxed),
            write_sectors: self.write_sectors.load(Ordering::Relaxed),
            in_flight: self.in_flight.load(Ordering::Relaxed),
        }
    }
}

static STATS: BlockingMutex<BTreeMap<u32, Arc<IoStats>>> = BlockingMutex::new(BTreeMap::new());

/// Creates the counters for a device that is about to register.
pub(crate) fn register(raw_id: u32) -> Arc<IoStats> {
    let stats = Arc::new(IoStats::default());
    STATS.lock().insert(raw_id, stats.clone());
    stats
}

/// Drops the counters of a device that is going away.
pub(crate) fn unregister(raw_id: u32) {
    STATS.lock().remove(&raw_id);
}

/// The counters of a registered device, for the files that report them.
pub fn lookup(id: DeviceId) -> Option<Arc<IoStats>> {
    STATS.lock().get(&id.to_raw()).cloned()
}

/// The sector count a request covers, as the tools expect to see it.
pub(crate) fn sectors_of(range: &core::ops::Range<Sid>) -> u64 {
    range.end.to_raw().saturating_sub(range.start.to_raw())
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn failed_requests_are_not_counted_as_io() {
        let stats = IoStats::default();
        stats.begin();
        stats.finish(BioType::Read, 8, false);
        assert_eq!(stats.snapshot(), IoStatsSnapshot::default());

        stats.begin();
        stats.finish(BioType::Read, 8, true);
        let snapshot = stats.snapshot();
        assert_eq!(snapshot.reads, 1);
        assert_eq!(snapshot.read_sectors, 8);
        assert_eq!(snapshot.in_flight, 0);
    }

    #[ktest]
    fn reads_and_writes_count_separately() {
        let stats = IoStats::default();
        stats.begin();
        stats.finish(BioType::Write, 16, true);
        stats.begin();
        stats.finish(BioType::Read, 4, true);
        let snapshot = stats.snapshot();
        assert_eq!((snapshot.reads, snapshot.writes), (1, 1));
        assert_eq!((snapshot.read_sectors, snapshot.write_sectors), (4, 16));
    }
}
