// SPDX-License-Identifier: MPL-2.0

//! The per-CPU record ring that every trace event writes into.
//!
//! A record is a fixed-size slot so that overwriting the oldest entry costs
//! nothing: the writer moves `head` forward and lets the reader work out which
//! slots are still live (`head - RING_SLOTS .. head`). Each CPU owns its ring
//! and takes it under a spinlock with local interrupts disabled, which is also
//! what makes an event emitted from an interrupt handler safe.

use core::{sync::atomic::AtomicBool, time::Duration};

use core::sync::atomic::Ordering;
use ostd::{
    cpu::PinCurrentCpu,
    sync::{LocalIrqDisabled, SpinLock},
    util::id_set::Id,
};

/// Payload bytes a record can carry. Longer messages are truncated; the
/// emitters format into this budget.
pub const RECORD_DATA_LEN: usize = 96;

/// Records kept per CPU (512 records is about 60 KiB per CPU).
pub const RING_SLOTS: usize = 512;

/// Upper bound on the CPUs this ring has storage for. A machine with more CPUs
/// drops records rather than indexing out of bounds.
pub const MAX_CPUS: usize = 8;

#[derive(Clone, Copy)]
struct Slot {
    timestamp: Duration,
    pid: u32,
    tid: u32,
    event: u16,
    len: u16,
    cpu: u8,
    data: [u8; RECORD_DATA_LEN],
}

impl Slot {
    const EMPTY: Self = Self {
        timestamp: Duration::ZERO,
        pid: 0,
        tid: 0,
        event: 0,
        len: 0,
        cpu: 0,
        data: [0; RECORD_DATA_LEN],
    };
}

struct CpuRing {
    slots: [Slot; RING_SLOTS],
    /// Records ever written to this CPU's ring; the live window is
    /// `head.saturating_sub(RING_SLOTS)..head`.
    head: u64,
}

impl CpuRing {
    const fn new() -> Self {
        Self {
            slots: [Slot::EMPTY; RING_SLOTS],
            head: 0,
        }
    }

    /// Index of the `count`-th oldest live slot, `count < RING_SLOTS`.
    fn slot_at(&self, index: u64) -> &Slot {
        &self.slots[(index % RING_SLOTS as u64) as usize]
    }
}

static RINGS: [SpinLock<CpuRing, LocalIrqDisabled>; MAX_CPUS] =
    [const { SpinLock::new(CpuRing::new()) }; MAX_CPUS];

/// Non-zero while this CPU is inside the ring (writing or reading it).
///
/// The ring's lock disables interrupts, and the code that runs under it is
/// instrumented like everything else: with the function tracer armed, a plain
/// `memcpy` inside the locked region calls `mcount`, which would try to take
/// the very lock it is running under -- a deadlock on one CPU, with interrupts
/// off. While the flag is set, `push` drops the record instead.
static IN_RING: [AtomicBool; MAX_CPUS] = [const { AtomicBool::new(false) }; MAX_CPUS];

const RING_ALREADY_ACTIVE: bool = true;

/// The current CPU, if this machine has a ring for it.
fn current_cpu() -> Option<usize> {
    let guard = ostd::task::disable_preempt();
    let cpu = guard.current_cpu().as_usize();
    (cpu < MAX_CPUS).then_some(cpu)
}

/// One record, as handed to a reader.
pub struct Record<'a> {
    pub timestamp: Duration,
    pub pid: u32,
    pub tid: u32,
    pub event: u16,
    pub cpu: u8,
    pub data: &'a [u8],
}

/// Appends a record to the current CPU's ring.
///
/// Called with preemption disabled and local interrupts off (the ring's
/// guardian) so that a record emitted from an interrupt handler cannot
/// interleave with one emitted by the task it interrupted.
pub fn push(event: u16, pid: u32, tid: u32, data: &[u8]) {
    push_at(event, pid, tid, data, aster_time::read_monotonic_time());
}

/// Like [`push`], but with the caller supplying the timestamp.
///
/// `read_monotonic_time` takes the clocksource's read lock, and an emitter
/// that can run *inside* the clocksource's update -- the function tracer runs
/// in every function, including the clock's own update path -- would then wait
/// on a lock its own CPU holds, with interrupts off. Such emitters pass a
/// lock-free timestamp instead (`Jiffies`).
pub fn push_at(event: u16, pid: u32, tid: u32, data: &[u8], timestamp: Duration) {
    let Some(cpu) = current_cpu() else { return };
    if IN_RING[cpu].swap(RING_ALREADY_ACTIVE, Ordering::Relaxed) {
        return;
    }

    let mut ring = RINGS[cpu].lock();
    let len = data.len().min(RECORD_DATA_LEN);
    let index = (ring.head % RING_SLOTS as u64) as usize;
    let slot = &mut ring.slots[index];
    slot.timestamp = timestamp;
    slot.pid = pid;
    slot.tid = tid;
    slot.event = event;
    slot.len = len as u16;
    slot.cpu = cpu as u8;
    slot.data[..len].copy_from_slice(&data[..len]);
    ring.head += 1;
    drop(ring);
    IN_RING[cpu].store(false, Ordering::Relaxed);
}

/// The current `head` of every CPU's ring.
///
/// A reader takes this once and then reads up to those bounds, so a workload
/// that keeps producing records cannot make the file endless.
pub fn heads() -> [u64; MAX_CPUS] {
    let mut heads = [0u64; MAX_CPUS];
    for (cpu, ring) in RINGS.iter().enumerate() {
        heads[cpu] = ring.lock().head;
    }
    heads
}

/// Visits every live record, oldest first within each CPU, until `visit`
/// returns `false` (a reader whose buffer filled up) or there are none left.
///
/// Records of different CPUs are not merged by timestamp; a reader that needs
/// that can sort what it collected.
pub fn for_each_record(visit: impl FnMut(Record<'_>) -> bool) {
    for_each_record_bounded(visit, heads())
}

/// How many records are copied out of a ring before the lock is dropped.
///
/// Readers must not call into the rest of the kernel while holding a ring
/// lock: the lock disables interrupts, and formatting a line writes to the
/// reader's buffer, which can fault and ask for a TLB flush -- a remote flush
/// on a CPU with interrupts off is a deadlock, and `ostd` turns it into a
/// panic ("Waiting for remote flush with IRQs disabled"). So the lock covers
/// only a copy of the slots; the visitor runs with it released.
const BATCH: usize = 32;

/// Like [`for_each_record`], but stops at the given per-CPU bounds.
pub fn for_each_record_bounded(mut visit: impl FnMut(Record<'_>) -> bool, bounds: [u64; MAX_CPUS]) {
    let mut batch = [Slot::EMPTY; BATCH];

    // Reading locks this CPU's ring too, and the reader's own function calls
    // are instrumented; see `IN_RING`.
    let Some(self_cpu) = current_cpu() else { return };
    if IN_RING[self_cpu].swap(RING_ALREADY_ACTIVE, Ordering::Relaxed) {
        return;
    }

    for (cpu, ring) in RINGS.iter().enumerate() {
        let (bound, mut cursor) = {
            let ring = ring.lock();
            let bound = bounds[cpu].min(ring.head);
            (bound, bound.saturating_sub(RING_SLOTS as u64))
        };

        while cursor < bound {
            let taken = {
                let ring = ring.lock();
                let mut taken = 0;
                while taken < BATCH && cursor < bound {
                    batch[taken] = *ring.slot_at(cursor);
                    taken += 1;
                    cursor += 1;
                }
                taken
            };
            if taken == 0 {
                break;
            }
            // The lock is released here: the visitor may fault or block.
            for slot in &batch[..taken] {
                let keep_going = visit(Record {
                    timestamp: slot.timestamp,
                    pid: slot.pid,
                    tid: slot.tid,
                    event: slot.event,
                    cpu: slot.cpu,
                    data: &slot.data[..slot.len as usize],
                });
                if !keep_going {
                    IN_RING[self_cpu].store(false, Ordering::Relaxed);
                    return;
                }
            }
        }
    }
    IN_RING[self_cpu].store(false, Ordering::Relaxed);
}

/// Empties every ring.
pub fn clear() {
    for ring in RINGS.iter() {
        ring.lock().head = 0;
    }
}

#[cfg(ktest)]
mod test {
    use super::*;

    /// Reads the payloads currently in the ring, in visit order.
    fn payloads() -> alloc::vec::Vec<alloc::string::String> {
        let mut out = alloc::vec::Vec::new();
        for_each_record(|record| {
            out.push(alloc::string::String::from_utf8_lossy(record.data).into_owned());
            true
        });
        out
    }

    #[ktest]
    fn records_round_trip() {
        clear();
        push(1, 10, 11, b"one");
        push(1, 20, 21, b"two");

        let mut seen = alloc::vec::Vec::new();
        for_each_record(|record| {
            seen.push((record.pid, record.tid, record.data.len()));
            true
        });
        assert_eq!(seen.len(), 2, "one record per push");
        assert_eq!(seen[0], (10, 11, 3));
        assert_eq!(seen[1], (20, 21, 3));
        assert_eq!(payloads(), ["one", "two"]);
    }

    #[ktest]
    fn overwrite_drops_the_oldest() {
        clear();
        let total = RING_SLOTS + 5;
        for index in 0..total {
            let pid = index as u32;
            push(1, pid, pid, b"x");
        }

        let mut pids = alloc::vec::Vec::new();
        for_each_record(|record| {
            pids.push(record.pid);
            true
        });
        assert_eq!(pids.len(), RING_SLOTS, "a full ring keeps RING_SLOTS records");
        assert_eq!(pids[0], 5, "the oldest five were overwritten");
        assert_eq!(pids[RING_SLOTS - 1], (total - 1) as u32);
    }

    #[ktest]
    fn bounds_stop_the_reader() {
        clear();
        let head = heads();
        push(1, 1, 1, b"after");
        // A reader that took its bounds before the push must not see it.
        let mut seen = 0;
        for_each_record_bounded(
            |_| {
                seen += 1;
                true
            },
            head,
        );
        assert_eq!(seen, 0);
    }

    #[ktest]
    fn clear_empties_the_ring() {
        push(1, 1, 1, b"gone");
        clear();
        assert_eq!(payloads().len(), 0);
    }
}
