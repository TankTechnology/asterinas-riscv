// SPDX-License-Identifier: MPL-2.0

//! The copy-on-write decision a write fault makes.
//!
//! `VmMapping::handle_single_page_fault` asks this crate what to do, so the
//! rule that decides between "upgrade the mapping in place" and "copy the
//! frame" is one small `unsafe`-free function that can be model-checked — see
//! `proofs` (run with `cargo kani`) and `tests` below.
//!
//! The rule, in the kernel's terms:
//!
//! * a read fault, or a mapping that is already writable, needs nothing;
//! * a shared (`MAP_SHARED`) mapping is upgraded in place: its writes are
//!   meant to be visible through every mapping of the frame;
//! * a private mapping is upgraded in place only when `refcount == 1`, i.e.
//!   when this mapping is the frame's sole reference;
//! * otherwise the frame must be copied.

#![no_std]

/// What the fault handler must do with the mapping that faulted.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CowAction {
    /// The access needs no change: it is a read, or the mapping is writable.
    None,
    /// Make the existing mapping writable; the frame is private to it.
    Upgrade,
    /// Allocate a private copy of the frame and map that instead.
    Copy,
}

/// Decides what a fault on a mapping must do.
///
/// `refcount` is the frame's reference count (`Frame::reference_count()`):
/// one per mapping plus one per page-cache handle.
pub fn decide(is_write: bool, writable: bool, is_shared: bool, refcount: usize) -> CowAction {
    if !is_write || writable {
        return CowAction::None;
    }
    if is_shared || refcount == 1 {
        CowAction::Upgrade
    } else {
        CowAction::Copy
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_and_writable_mappings_do_nothing() {
        assert_eq!(decide(false, false, false, 2), CowAction::None);
        assert_eq!(decide(false, true, true, 2), CowAction::None);
        assert_eq!(decide(true, true, false, 2), CowAction::None);
    }

    #[test]
    fn shared_mappings_upgrade_in_place() {
        assert_eq!(decide(true, false, true, 2), CowAction::Upgrade);
        assert_eq!(decide(true, false, true, 7), CowAction::Upgrade);
    }

    #[test]
    fn private_frames_copy_unless_solely_referenced() {
        assert_eq!(decide(true, false, false, 1), CowAction::Upgrade);
        assert_eq!(decide(true, false, false, 2), CowAction::Copy);
        assert_eq!(decide(true, false, false, 9), CowAction::Copy);
    }
}

/// Bounded proofs with Kani (`cargo kani --lib`).
///
/// The central property: for a private mapping, upgrading in place is only
/// ever chosen when the mapping is the frame's sole reference. Any other
/// choice would let two address spaces write one frame — the CoW violation the
/// TLA+ model (`tools/riscv/concurrency-models/fork-spec`) checks at the
/// design level.
#[cfg(kani)]
mod proofs {
    use super::*;

    #[kani::proof]
    fn upgrade_only_for_shared_or_sole_reference() {
        let is_write: bool = kani::any();
        let writable: bool = kani::any();
        let is_shared: bool = kani::any();
        let refcount: usize = kani::any();
        kani::assume(refcount >= 1);

        if decide(is_write, writable, is_shared, refcount) == CowAction::Upgrade && !is_shared {
            assert_eq!(refcount, 1);
        }
    }

    #[kani::proof]
    fn a_shared_private_frame_is_always_copied() {
        let is_write: bool = kani::any();
        let writable: bool = kani::any();
        let is_shared: bool = kani::any();
        let refcount: usize = kani::any();
        kani::assume(is_write && !writable && !is_shared && refcount > 1);

        assert_eq!(decide(is_write, writable, is_shared, refcount), CowAction::Copy);
    }

    #[kani::proof]
    fn shared_mappings_never_copy() {
        let is_write: bool = kani::any();
        let writable: bool = kani::any();
        let refcount: usize = kani::any();
        kani::assume(is_write && !writable);

        assert_eq!(decide(is_write, writable, true, refcount), CowAction::Upgrade);
    }
}
