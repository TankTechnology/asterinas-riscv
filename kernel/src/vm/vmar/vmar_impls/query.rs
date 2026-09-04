// SPDX-License-Identifier: MPL-2.0

use core::ops::Range;

use ostd::{sync::RwMutexReadGuard, task::disable_preempt};

use super::{VMAR_CAP_ADDR, VmMapping, Vmar, VmarInner};
use crate::prelude::*;

impl Vmar {
    /// Finds all the mapped regions that intersect with the specified range.
    pub fn query(&self, range: Range<usize>) -> VmarQueryGuard<'_> {
        VmarQueryGuard {
            vmar: self.inner.read(),
            range,
        }
    }

    /// Returns whether a page is currently present in this address space's page table.
    ///
    /// This method only observes the page table. It does not trigger a page fault for a
    /// lazily allocated mapping.
    pub fn is_page_present(&self, page_addr: Vaddr) -> Result<bool> {
        if !page_addr.is_multiple_of(PAGE_SIZE) || page_addr >= VMAR_CAP_ADDR {
            return_errno_with_message!(Errno::EINVAL, "the page address is outside userspace");
        }

        let preempt_guard = disable_preempt();
        let page_end = page_addr
            .checked_add(PAGE_SIZE)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "the page address overflows"))?;
        let mut cursor = self
            .vm_space()
            .cursor(&preempt_guard, &(page_addr..page_end))?;

        Ok(cursor.query()?.1.is_some())
    }
}

/// A guard that allows querying a [`Vmar`] for its mappings.
pub struct VmarQueryGuard<'a> {
    vmar: RwMutexReadGuard<'a, VmarInner>,
    range: Range<usize>,
}

impl VmarQueryGuard<'_> {
    /// Returns an iterator over the [`VmMapping`]s that intersect with the
    /// provided range when calling [`Vmar::query`].
    pub fn iter(&self) -> impl Iterator<Item = &VmMapping> {
        self.vmar.query(&self.range)
    }

    /// Returns whether the range is fully mapped.
    ///
    /// In other words, this method will return `false` if and only if the
    /// range contains pages that are not mapped.
    pub fn is_fully_mapped(&self) -> bool {
        let mut last_mapping_end = self.range.start;

        for mapping in self.iter() {
            if last_mapping_end < mapping.map_to_addr() {
                return false;
            }
            last_mapping_end = mapping.map_end();
        }

        if last_mapping_end < self.range.end {
            return false;
        }

        true
    }
}
