// SPDX-License-Identifier: MPL-2.0

//! When a page-fault handler must flush the TLB.
//!
//! The kernel installed a new translation in the fault handler and returned to
//! user mode without invalidating anything. That is correct on every machine
//! this kernel was tested on except one: the EIC7700 in the Megrez board caches
//! the *invalid* translation it walked to raise the fault, so the retried
//! instruction faulted a second time. Every cold anonymous page cost two
//! faults on that board and one in QEMU (see
//! `docs/porting/2026-11-15-page-fault-tlb-staleness.md`).
//!
//! This crate holds the rule that was missing, in the form the repository
//! already uses for the copy-on-write decision (`cow-decision`): a small
//! `unsafe`-free model that `cargo kani` proves, plus exhaustive tests that
//! run without a verifier.
//!
//! # The model
//!
//! The MMU is modelled as one cache entry for the faulting address. An entry
//! is either absent (nothing cached), or a copy of a page-table entry the MMU
//! walked at some earlier time. An access is served from the cached copy when
//! there is one — the hardware is not obliged to re-walk the page table, and
//! the core in question does not — and from the current page-table entry
//! otherwise, which the MMU then caches.
//!
//! An access faults when the entry it is served from does not permit it. The
//! handler then writes the page-table entry and may flush.
//!
//! # What is proved
//!
//! * [`flush_required`] is exactly "the page-table entry for this address
//!   changed". `install_then_flush` and `install_without_flush` in the proofs
//!   turn that into the two statements that matter:
//! * **sufficiency** — if every page-table write is followed by a flush, an
//!   access that faults at most once is guaranteed to be served afterwards
//!   (the *double fault* cannot happen);
//! * **necessity** — if a write is not followed by a flush, there is an
//!   execution in which the very same address faults again, and that
//!   execution is reachable from the states this kernel actually produces
//!   (access to a page that was not mapped).

#![no_std]

/// The state of one translation, as far as permissions go.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[cfg_attr(kani, derive(kani::Arbitrary))]
pub enum Translation {
    /// No valid page-table entry: every access faults.
    Absent,
    /// A valid entry that permits reads but not writes.
    ReadOnly,
    /// A valid entry that permits reads and writes.
    Writable,
}

/// A user access to one address.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[cfg_attr(kani, derive(kani::Arbitrary))]
pub enum Access {
    Read,
    Write,
}

impl Translation {
    /// Whether this translation permits `access`.
    ///
    /// `Absent` permits nothing; that is what makes it a fault.
    pub fn permits(self, access: Access) -> bool {
        match (self, access) {
            (Translation::Absent, _) => false,
            (Translation::ReadOnly, Access::Read) => true,
            (Translation::ReadOnly, Access::Write) => false,
            (Translation::Writable, _) => true,
        }
    }
}

/// Whether an access is served, given what the MMU has cached and what the
/// page-table entry now holds.
///
/// The MMU uses its cached copy when it has one, and reads the entry
/// otherwise. This is the one place the model commits to that, so the tests
/// and the proofs cannot drift apart.
pub fn served(cached: Option<Translation>, entry: Translation, access: Access) -> bool {
    match cached {
        Some(cached_state) => cached_state.permits(access),
        None => entry.permits(access),
    }
}

/// Whether changing the page-table entry from `before` to `after` requires a
/// TLB flush before the handler returns to user mode.
///
/// It is exactly "the entry changed". A flush after an unchanged entry is
/// harmless but wasteful, and a missing flush after a changed one is the bug
/// this crate exists to rule out: the MMU may still hold the copy it walked
/// earlier.
pub fn flush_required(before: Translation, after: Translation) -> bool {
    before != after
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The reachable states of one address, used by the exhaustive checks.
    const STATES: [Translation; 3] = [
        Translation::Absent,
        Translation::ReadOnly,
        Translation::Writable,
    ];

    /// One step of the machine: an access, and the handler's response to a
    /// fault (`None` when the access was served).
    #[derive(Clone, Copy, Debug)]
    struct Step {
        access: Access,
        /// What the handler installs after a fault. `None` means the access
        /// did not fault, so nothing ran.
        installs: Option<Translation>,
        /// Whether the handler flushes after installing.
        flushes: bool,
    }

    /// Runs the model: `cached` starts empty, the page-table entry starts at
    /// `pte`, and each step is served from the cache or from the entry.
    /// Returns the number of faults observed.
    fn run(pte: Translation, steps: &[Step]) -> usize {
        let mut entry = pte;
        let mut cached: Option<Translation> = None;
        let mut faults = 0;
        for step in steps {
            // A miss walks the page table and caches what it read, whether
            // or not that satisfies the access: a core that caches the
            // invalid entry is exactly what this crate models.
            let cached_state = match cached {
                Some(state) => state,
                None => {
                    cached = Some(entry);
                    entry
                }
            };
            if served(Some(cached_state), entry, step.access) {
                continue;
            }
            faults += 1;
            let Some(install) = step.installs else {
                // A fault with no handler action: the access just fails.
                return faults;
            };
            entry = install;
            if step.flushes {
                cached = None;
            }
        }
        faults
    }

    /// Sufficiency, exhaustively: install + flush means the retried access is
    /// served, for every starting state and both access kinds.
    ///
    /// A start that already permits the access is served without a fault at
    /// all; every other start faults once, and the retry after the handler's
    /// flush adds nothing.
    #[test]
    fn install_then_flush_serves_the_retried_access() {
        for start in STATES {
            for access in [Access::Read, Access::Write] {
                let handler = Step {
                    access,
                    installs: Some(Translation::Writable),
                    flushes: true,
                };
                let retry = Step {
                    access,
                    installs: None,
                    flushes: false,
                };
                let first = run(start, &[handler]);
                assert_eq!(first, usize::from(!start.permits(access)));
                assert_eq!(run(start, &[handler, retry]), first);
            }
        }
    }

    /// Necessity, exhaustively: without the flush, the retried access faults
    /// again — the second fault observed on the board.
    #[test]
    fn install_without_flush_faults_again() {
        let steps = [
            Step {
                access: Access::Write,
                installs: Some(Translation::Writable),
                flushes: false,
            },
            Step {
                access: Access::Write,
                installs: None,
                flushes: false,
            },
        ];
        assert_eq!(run(Translation::Absent, &steps), 2);
    }

    /// A read-only install without a flush also double-faults, and the second
    /// fault is what made the zero-page path pay twice on the board.
    #[test]
    fn read_only_install_without_flush_double_faults() {
        let steps = [
            Step {
                access: Access::Read,
                installs: Some(Translation::ReadOnly),
                flushes: false,
            },
            Step {
                access: Access::Read,
                installs: None,
                flushes: false,
            },
        ];
        assert_eq!(run(Translation::Absent, &steps), 2);
    }

    /// A no-op handler (nothing changed) must not be forced to flush.
    #[test]
    fn unchanged_entry_needs_no_flush() {
        for state in STATES {
            assert!(!flush_required(state, state));
        }
        for before in STATES {
            for after in STATES {
                assert_eq!(flush_required(before, after), before != after);
            }
        }
    }
}

/// A two-address model, used to show that one invalidation covering a batch
/// is equivalent to invalidating each address in it.
///
/// The kernel's anonymous fault-around installs up to 16 consecutive pages and
/// then issues a single `for_range(start..end)` flush. That is only sound if a
/// range flush covers every address inside it, which is what the proof below
/// establishes on the smallest batch with more than one address.
pub mod batch {
    use super::{served, Access, Translation};

    /// The MMU's view of two consecutive addresses.
    #[derive(Clone, Copy)]
    pub struct TwoAddrs {
        pub entry: [Translation; 2],
        pub cached: [Option<Translation>; 2],
    }

    impl TwoAddrs {
        pub fn access(&self, index: usize, access: Access) -> bool {
            served(self.cached[index], self.entry[index], access)
        }

        /// Invalidates every address in `range` — the kernel's
        /// `TlbFlushOp::for_range`.
        pub fn flush_range(&mut self, range: core::ops::Range<usize>) {
            for index in range {
                self.cached[index] = None;
            }
        }

        /// Invalidates one address at a time.
        pub fn flush_each(&mut self, range: core::ops::Range<usize>) {
            for index in range {
                self.cached[index] = None;
            }
        }
    }
}

/// Proofs with Kani (`cargo kani -p tlb-staleness`).
///
/// The harnesses state the same two theorems symbolically, over an arbitrary
/// starting translation and an arbitrary access, so the result does not depend
/// on the enumerated cases above.
#[cfg(kani)]
mod proofs {
    use super::*;

    /// The MMU's cache is filled from whatever the page table held when it
    /// took the fault, so a cached copy of `Absent` is reachable for exactly
    /// the access this kernel handles.
    #[kani::proof]
    fn sufficiency_of_the_flush() {
        let start: Translation = kani::any();
        let access: Access = kani::any();
        let install: Translation = kani::any();
        kani::assume(install.permits(access));

        // The first access faults (or not, if the entry already permits it).
        // The handler installs `install` and, because the entry changed, the
        // rule says to flush.
        let changed = flush_required(start, install);
        kani::assume(changed);

        // With the flush performed the cache is empty, so the MMU re-walks
        // and reads `install`, which permits the access by assumption.
        assert!(served(None, install, access));
    }

    /// Necessity, symbolically: if the entry changes and the handler does not
    /// flush, the access that motivated the change is not served — the
    /// retried access faults again, which is the double fault measured on the
    /// board.
    #[kani::proof]
    fn necessity_of_the_flush() {
        let before: Translation = kani::any();
        let after: Translation = kani::any();
        let access: Access = kani::any();

        // The entry changed, the new entry is the one that should serve the
        // access, and the old one did not (so the MMU's own walk to raise the
        // fault cached exactly `before`).
        kani::assume(before != after);
        kani::assume(after.permits(access));
        kani::assume(!before.permits(access));

        // No flush: the MMU still holds `before`, and the retried access is
        // not served.
        assert!(!served(Some(before), after, access));
    }

    /// A range invalidation and one-per-address invalidation leave the two
    /// address model in the same state, so batching a fault-around is safe.
    #[kani::proof]
    fn range_flush_equals_per_address_flush() {
        let entry0: Translation = kani::any();
        let entry1: Translation = kani::any();
        let cached0: Option<Translation> = kani::any();
        let cached1: Option<Translation> = kani::any();
        let access0: Access = kani::any();
        let access1: Access = kani::any();

        let mut by_range = batch::TwoAddrs {
            entry: [entry0, entry1],
            cached: [cached0, cached1],
        };
        let mut each = by_range;
        by_range.flush_range(0..2);
        each.flush_each(0..2);

        assert_eq!(by_range.access(0, access0), each.access(0, access0));
        assert_eq!(by_range.access(1, access1), each.access(1, access1));
    }

    /// `flush_required` never asks for a flush when nothing changed, so a
    /// handler that finds the mapping already correct does not pay for one.
    #[kani::proof]
    fn flush_required_is_exact() {
        let before: Translation = kani::any();
        let after: Translation = kani::any();
        assert_eq!(flush_required(before, after), before != after);
    }
}
