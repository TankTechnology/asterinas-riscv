// SPDX-License-Identifier: MPL-2.0

use core::ops::Range;

use ostd::{mm::VmSpace, sync::RwMutexReadGuard, task::disable_preempt};

use super::{VMAR_CAP_ADDR, VmMapping, Vmar, VmarInner};
use crate::{fs::file::Permission, prelude::*, process::Uid};

impl Vmar {
    /// Finds all the mapped regions that intersect with the specified range.
    pub fn query(&self, range: Range<usize>) -> VmarQueryGuard<'_> {
        VmarQueryGuard {
            vmar: self.inner.read(),
            vm_space: self.vm_space(),
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
    vm_space: &'a VmSpace,
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

    /// Writes one `mincore(2)` residency byte for every page in the query range.
    ///
    /// The query range must be page-aligned and match the output slice exactly.
    /// An unmapped page causes `ENOMEM`. No page is faulted in by this method.
    pub fn fill_page_residency(&self, residency: &mut [u8], fsuid: Uid) -> Result<()> {
        debug_assert!(self.range.start.is_multiple_of(PAGE_SIZE));
        debug_assert!(self.range.end.is_multiple_of(PAGE_SIZE));
        debug_assert_eq!(self.range.len() / PAGE_SIZE, residency.len());

        let mut next_page_addr = self.range.start;
        let mut output_idx = 0;

        for mapping in self.iter() {
            if next_page_addr < mapping.map_to_addr() {
                return_errno_with_message!(
                    Errno::ENOMEM,
                    "the range contains pages that are not mapped"
                );
            }

            let mapping_end = mapping.map_end().min(self.range.end);
            let can_reveal_page_cache = can_reveal_page_cache(mapping, fsuid);
            while next_page_addr < mapping_end {
                let is_present = self.vmar_is_page_present(next_page_addr)?;
                let is_resident = is_present
                    || !can_reveal_page_cache
                    || backing_page_is_resident(mapping, next_page_addr);
                residency[output_idx] = u8::from(is_resident);

                next_page_addr += PAGE_SIZE;
                output_idx += 1;
            }
        }

        if next_page_addr < self.range.end {
            return_errno_with_message!(
                Errno::ENOMEM,
                "the range contains pages that are not mapped"
            );
        }

        Ok(())
    }

    fn vmar_is_page_present(&self, page_addr: Vaddr) -> Result<bool> {
        let preempt_guard = disable_preempt();
        let mut cursor = self
            .vm_space
            .cursor(&preempt_guard, &(page_addr..page_addr + PAGE_SIZE))?;

        Ok(cursor.query()?.1.is_some())
    }
}

fn can_reveal_page_cache(mapping: &VmMapping, fsuid: Uid) -> bool {
    let Some(inode) = mapping.inode() else {
        return true;
    };

    inode.metadata().is_ok_and(|metadata| metadata.uid == fsuid)
        || inode.check_permission(Permission::MAY_WRITE).is_ok()
}

fn backing_page_is_resident(mapping: &VmMapping, page_addr: Vaddr) -> bool {
    let Some(mapped_vmo) = mapping.vmo() else {
        return false;
    };

    let offset_in_mapping = page_addr - mapping.map_to_addr();
    let Some(vmo_offset) = mapped_vmo.offset().checked_add(offset_in_mapping) else {
        return false;
    };
    mapped_vmo.vmo().is_page_resident(vmo_offset)
}
