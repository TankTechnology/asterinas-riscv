// SPDX-License-Identifier: MPL-2.0

//! Loom model of the copy-on-write protocol behind `fork`.
//!
//! Real code:
//!
//! * `kernel/src/vm/vmar/vmar_impls/fork.rs` — `cow_copy_pt` makes the
//!   parent's mappings read-only and maps the *same* frames into the child's
//!   page table.
//! * `kernel/src/vm/vmar/vm_mapping.rs` — `handle_single_page_fault` performs
//!   the write-fault half: if the frame has more than one reference it copies;
//!   if `frame.reference_count() == 1` it simply upgrades the PTE to writable.
//!
//! The invariant is the one CoW exists for:
//!
//!   **A writable mapping's frame is otherwise unreferenced.** No other
//!   address space maps it and the VMO page cache does not hold it. Otherwise
//!   a write through one mapping would be visible through another.
//!
//! The model covers the three operations that can race after a fork: a write
//! fault (upgrade in place or copy), an unmap (drops one reference), and a
//! read fault that re-maps the frame from the page cache. Each address
//! space has its own page-table lock (`CursorMut` in the kernel); the frame's
//! reference count and the cache flag are loom atomics.
//!
//! What this does not cover is listed in the README: TLB shootdown ordering,
//! the `handle_page_faults_around` batching, and everything outside this
//! protocol (process creation, signal state, file tables).

#![cfg(test)]

use std::sync::atomic::{AtomicBool as StdAtomicBool, Ordering as StdOrdering};
use std::sync::Arc;

use loom::sync::atomic::{AtomicIsize, Ordering};
use loom::sync::Mutex;
use loom::thread;

/// The one frame that `fork` shares between the parent and the child.
const SHARED_FRAME: usize = 0;

#[derive(Clone, Copy, Debug)]
enum Action {
    /// `handle_single_page_fault` with write permission.
    WriteFault,
    /// `munmap` or process exit: drop this address space's reference.
    Unmap,
    /// A read fault that maps the frame back in from the page cache.
    RemapFromCache,
}

#[derive(Clone, Copy, Debug, Default)]
struct Pte {
    frame: Option<usize>,
    writable: bool,
}

struct AddressSpace {
    /// The page table, behind its lock (the kernel's `CursorMut`).
    pte: Mutex<Pte>,
}

struct Shared {
    /// `frame.reference_count()` for the shared frame: one per mapping plus
    /// one when the VMO page cache holds it.
    refs: AtomicIsize,
    /// Whether the VMO page cache holds the frame.
    cached: AtomicIsize,
    /// Source of unique ids for copied frames (a fresh allocation).
    next_copy: AtomicIsize,
    parent: AddressSpace,
    child: AddressSpace,
    /// An interleaving that broke the invariant was reached.
    violation: StdAtomicBool,
}

impl Shared {
    /// `cached` says whether the page cache held the frame at fork time, i.e.
    /// whether a read fault can map it back after an unmap.
    fn new(cached: bool) -> Self {
        Self {
            refs: AtomicIsize::new(2 + cached as isize),
            cached: AtomicIsize::new(cached as isize),
            next_copy: AtomicIsize::new(1),
            parent: AddressSpace {
                pte: Mutex::new(Pte {
                    frame: Some(SHARED_FRAME),
                    writable: false,
                }),
            },
            child: AddressSpace {
                pte: Mutex::new(Pte {
                    frame: Some(SHARED_FRAME),
                    writable: false,
                }),
            },
            violation: StdAtomicBool::new(false),
        }
    }

    /// A write fault in `space` (`handle_single_page_fault`).
    ///
    /// With `fixed == false` the model omits the `reference_count() == 1`
    /// check and always upgrades the PTE in place, which is the bug the
    /// invariant is meant to catch.
    fn write_fault(&self, space: &AddressSpace, fixed: bool) {
        let mut pte = space.pte.lock().unwrap();
        if pte.writable || pte.frame.is_none() {
            return;
        }

        let only_reference = self.refs.load(Ordering::Acquire) == 1;

        if !fixed || only_reference {
            // Upgrade in place: legal only when nothing else references the frame.
            pte.writable = true;
            drop(pte);
            if !only_reference {
                self.violation.store(true, StdOrdering::Release);
            }
        } else {
            // Copy: the new frame belongs to this address space alone, so its
            // reference count is one and it is never entered in `refs`.
            let new_frame = self.next_copy.fetch_add(1, Ordering::Relaxed) as usize;
            self.refs.fetch_sub(1, Ordering::AcqRel);
            pte.frame = Some(new_frame);
            pte.writable = true;
        }
    }

    fn unmap(&self, space: &AddressSpace) {
        let mut pte = space.pte.lock().unwrap();
        if pte.frame == Some(SHARED_FRAME) {
            self.refs.fetch_sub(1, Ordering::AcqRel);
        }
        pte.frame = None;
        pte.writable = false;
    }

    /// Maps the shared frame back in from the page cache, read-only.
    fn remap_from_cache(&self, space: &AddressSpace) {
        let mut pte = space.pte.lock().unwrap();
        if pte.frame.is_some() {
            return;
        }
        // The cache's reference keeps the frame alive; taking a new mapping
        // reference must make the upgrade-in-place test fail while it is cached.
        if self.cached.load(Ordering::Acquire) == 0 {
            return;
        }
        self.refs.fetch_add(1, Ordering::AcqRel);
        pte.frame = Some(SHARED_FRAME);
        pte.writable = false;
    }

    /// The invariant: a writable mapping's frame is otherwise unreferenced.
    fn check_invariant(&self) -> bool {
        let parent = *self.parent.pte.lock().unwrap();
        let child = *self.child.pte.lock().unwrap();

        for pte in [parent, child] {
            if let (Some(frame), true) = (pte.frame, pte.writable) {
                if frame == SHARED_FRAME {
                    if self.refs.load(Ordering::Acquire) != 1 {
                        return false;
                    }
                    if self.cached.load(Ordering::Acquire) != 0 {
                        return false;
                    }
                }
            }
        }

        // Two mappings of one frame must never both be writable.
        if parent.frame == child.frame && parent.frame.is_some() && parent.writable && child.writable
        {
            return false;
        }

        true
    }
}

/// Runs every interleaving of `parent_ops` and `child_ops` and reports whether
/// any of them broke the invariant.
fn model(fixed: bool, cached: bool, parent_ops: &[Action], child_ops: &[Action]) -> bool {
    let shared = Arc::new(Shared::new(cached));

    let spawn = |space: usize, ops: Vec<Action>| {
        let shared = shared.clone();
        thread::spawn(move || {
            let target = if space == 0 {
                &shared.parent
            } else {
                &shared.child
            };
            for action in ops {
                match action {
                    Action::WriteFault => shared.write_fault(target, fixed),
                    Action::Unmap => shared.unmap(target),
                    Action::RemapFromCache => shared.remap_from_cache(target),
                }
                if !shared.check_invariant() {
                    shared.violation.store(true, StdOrdering::Release);
                }
            }
        })
    };

    let p = spawn(0, parent_ops.to_vec());
    let c = spawn(1, child_ops.to_vec());
    p.join().unwrap();
    c.join().unwrap();

    shared.violation.load(StdOrdering::Acquire) || !shared.check_invariant()
}

#[test]
fn buggy_protocol_allows_a_shared_writable_frame() {
    // Without the `reference_count() == 1` check, the parent upgrades its PTE
    // in place while the child still maps the frame: loom must find it.
    static FOUND: StdAtomicBool = StdAtomicBool::new(false);
    loom::model(|| {
        if model(false, false, &[Action::WriteFault], &[Action::WriteFault]) {
            FOUND.store(true, StdOrdering::Relaxed);
        }
    });
    assert!(
        FOUND.load(StdOrdering::Relaxed),
        "loom did not reach the shared-writable-PTE interleaving"
    );
}

#[test]
fn fixed_protocol_keeps_writable_frames_private() {
    // Both sides fault on the same page: whoever keeps the shared frame must
    // copy, so neither mapping ends up writable while shared.
    loom::model(|| {
        assert!(
            !model(true, false, &[Action::WriteFault], &[Action::WriteFault]),
            "a writable PTE shared its frame with another mapping"
        );
    });
}

#[test]
fn fixed_protocol_survives_fault_vs_unmap() {
    // The parent faults while the child unmaps: either the parent sees two
    // references and copies, or it sees one and upgrades in place.
    loom::model(|| {
        assert!(
            !model(true, false, &[Action::WriteFault], &[Action::Unmap]),
            "a fault racing with an unmap left a shared writable frame"
        );
    });
}

#[test]
fn fixed_protocol_survives_fault_vs_unmap_vs_remap() {
    // The page cache holds the frame, so after the child unmaps it can map the
    // frame back in while the parent is faulting. The parent must not upgrade
    // its PTE in place while the cache can still hand the frame out.
    loom::model(|| {
        assert!(
            !model(
                true,
                true,
                &[Action::WriteFault],
                &[Action::Unmap, Action::RemapFromCache, Action::WriteFault]
            ),
            "a page-cache remap resurrected a writable frame"
        );
    });
}
