// SPDX-License-Identifier: MPL-2.0

use core::ops::Range;

use super::{Interval, Vmar, VmarInner, util::get_intersected_range};
use crate::prelude::*;

impl Vmar {
    /// Returns the amount of virtual memory that is locked, in bytes.
    pub fn get_locked_size(&self) -> usize {
        self.inner.read().locked_size()
    }

    /// Locks every mapped page in `range`.
    ///
    /// `max_locked_size` is `None` when the caller may bypass `RLIMIT_MEMLOCK`.
    pub fn lock_memory(&self, range: Range<Vaddr>, max_locked_size: Option<usize>) -> Result<()> {
        debug_assert!(range.start.is_multiple_of(PAGE_SIZE));
        debug_assert!(range.end.is_multiple_of(PAGE_SIZE));

        let mut inner = self.inner.write();
        let mapped_prefix_end = inner.mapped_prefix_end(&range);
        let mapped_prefix = range.start..mapped_prefix_end;
        let additional_size = inner.unlocked_size_in_range(&mapped_prefix);
        inner.check_locked_size_limit(additional_size, max_locked_size)?;
        inner.set_mapping_lock(&mapped_prefix, true);

        if mapped_prefix_end < range.end {
            return_errno_with_message!(
                Errno::ENOMEM,
                "the range contains pages that are not mapped"
            );
        }
        Ok(())
    }

    /// Unlocks every mapped page in `range`.
    pub fn unlock_memory(&self, range: Range<Vaddr>) -> Result<()> {
        debug_assert!(range.start.is_multiple_of(PAGE_SIZE));
        debug_assert!(range.end.is_multiple_of(PAGE_SIZE));

        let mut inner = self.inner.write();
        let mapped_prefix_end = inner.mapped_prefix_end(&range);
        inner.set_mapping_lock(&(range.start..mapped_prefix_end), false);

        if mapped_prefix_end < range.end {
            return_errno_with_message!(
                Errno::ENOMEM,
                "the range contains pages that are not mapped"
            );
        }
        Ok(())
    }

    /// Applies an `mlockall` request to this address space.
    pub fn lock_all_memory(
        &self,
        lock_current: bool,
        lock_future: bool,
        max_locked_size: Option<usize>,
    ) -> Result<()> {
        let mut inner = self.inner.write();

        if lock_current {
            let additional_size = inner.total_vm.saturating_sub(inner.locked_size());
            inner.check_locked_size_limit(additional_size, max_locked_size)?;
            let full_range = 0..super::VMAR_CAP_ADDR;
            inner.set_mapping_lock(&full_range, true);
        }

        inner.lock_future = lock_future;
        inner.lock_future_limit = max_locked_size;
        Ok(())
    }

    /// Removes all memory locks and disables locking for future mappings.
    pub fn unlock_all_memory(&self) {
        let mut inner = self.inner.write();
        let full_range = 0..super::VMAR_CAP_ADDR;
        inner.set_mapping_lock(&full_range, false);
        inner.lock_future = false;
        inner.lock_future_limit = None;
    }
}

impl VmarInner {
    pub(super) fn new_mapping_lock_limit(
        &self,
        explicit_limit: Option<Option<usize>>,
    ) -> Option<Option<usize>> {
        explicit_limit.or_else(|| self.lock_future.then_some(self.lock_future_limit))
    }

    pub(super) fn check_new_locked_mapping(
        &self,
        range: &Range<Vaddr>,
        max_locked_size: Option<usize>,
    ) -> Result<()> {
        let replaced_locked_size = self.locked_size_in_range(range);
        let additional_size = range.len().saturating_sub(replaced_locked_size);
        self.check_locked_size_limit(additional_size, max_locked_size)
    }

    fn locked_size(&self) -> usize {
        self.vm_mappings
            .iter()
            .filter(|mapping| mapping.is_locked())
            .map(|mapping| mapping.map_size())
            .sum()
    }

    fn locked_size_in_range(&self, range: &Range<Vaddr>) -> usize {
        self.vm_mappings
            .find(range)
            .filter(|mapping| mapping.is_locked())
            .map(|mapping| get_intersected_range(range, &mapping.range()).len())
            .sum()
    }

    fn unlocked_size_in_range(&self, range: &Range<Vaddr>) -> usize {
        self.vm_mappings
            .find(range)
            .filter(|mapping| !mapping.is_locked())
            .map(|mapping| get_intersected_range(range, &mapping.range()).len())
            .sum()
    }

    fn check_locked_size_limit(
        &self,
        additional_size: usize,
        max_locked_size: Option<usize>,
    ) -> Result<()> {
        let Some(max_locked_size) = max_locked_size else {
            return Ok(());
        };

        if max_locked_size.saturating_sub(self.locked_size()) < additional_size {
            return_errno_with_message!(Errno::ENOMEM, "the locked memory limit is reached");
        }
        Ok(())
    }

    fn mapped_prefix_end(&self, range: &Range<Vaddr>) -> Vaddr {
        let mut last_mapping_end = range.start;
        for mapping in self.vm_mappings.find(range) {
            if last_mapping_end < mapping.map_to_addr() {
                break;
            }
            last_mapping_end = mapping.map_end().min(range.end);
        }
        last_mapping_end
    }

    fn set_mapping_lock(&mut self, range: &Range<Vaddr>, is_locked: bool) {
        if range.is_empty() {
            return;
        }

        let mapping_addresses: Vec<_> = self
            .vm_mappings
            .find(range)
            .map(|mapping| mapping.map_to_addr())
            .collect();

        for mapping_addr in mapping_addresses {
            let Some(mapping) = self.remove(&mapping_addr) else {
                // A preceding update may have merged this mapping into its neighbor.
                continue;
            };
            let intersected_range = get_intersected_range(range, &mapping.range());
            let (left, within, right) = mapping.split_range(&intersected_range);

            if let Some(left) = left {
                self.insert_without_try_merge(left);
            }
            if let Some(right) = right {
                self.insert_without_try_merge(right);
            }
            self.insert_try_merge(within.set_locked(is_locked));
        }
    }
}
