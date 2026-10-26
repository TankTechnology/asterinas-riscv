// SPDX-License-Identifier: MPL-2.0

#![short_vis_path::add(vmar)]

use super::{Interval, RssDelta, Vmar};
use crate::{prelude::*, vm::perms::VmPerms};

impl Vmar {
    pub(crate) fn handle_page_fault(&self, page_fault_info: &PageFaultInfo) -> Result<()> {
        // A user page fault can be taken while the faulting syscall holds spin
        // locks (for example, a socket holds its state lock while it copies
        // user memory). Waiting for the (sleepable) Vmar lock there would
        // deschedule the task in atomic mode, which the scheduler rejects with
        // "might break atomic mode". Never block in that context: if the lock
        // is contended, report the fault as unhandled so that the fallible
        // copy returns EFAULT instead of taking down the kernel.
        let inner = if ostd::task::atomic_mode::preempt_count() == 0 {
            self.inner.read()
        } else {
            self.inner.try_read().ok_or_else(|| {
                Error::with_message(Errno::EAGAIN, "the Vmar is locked in atomic mode")
            })?
        };

        let address = page_fault_info.address;
        if let Some(vm_mapping) = inner.vm_mappings.find_one(&address) {
            debug_assert!(vm_mapping.range().contains(&address));

            let mut rss_delta = RssDelta::new(self);
            return vm_mapping.handle_page_fault(&self.vm_space, page_fault_info, &mut rss_delta);
        }

        return_errno_with_message!(
            Errno::EACCES,
            "no VM mappings contain the page fault address"
        );
    }
}

/// Page fault information converted from [`CpuException`].
///
/// `TryFrom<CpuException>` should be implemented for this struct.
/// If [`CpuException`] is a page fault, `try_from` should return `Ok(PageFaultInfo)`,
/// or `Err(())` (no error information) otherwise.
///
/// [`CpuException`]: ostd::arch::cpu::context::CpuException
#[derive(Debug)]
pub(crate) struct PageFaultInfo {
    /// The virtual address where a page fault occurred.
    pub(crate) address: Vaddr,

    /// The [`VmPerms`] required by the memory operation that causes page fault.
    /// For example, a "store" operation may require `VmPerms::WRITE`.
    pub(in vmar) required_perms: VmPerms,

    /// Whether this page fault is forced (e.g., manually triggered by `ptrace`).
    /// A forced page fault may bypass some permission checks.
    is_forced: bool,
}

impl PageFaultInfo {
    /// Creates a new `PageFaultInfo`.
    pub(crate) fn new(address: Vaddr, required_perms: VmPerms) -> Self {
        Self {
            address,
            required_perms,
            is_forced: false,
        }
    }

    /// Returns whether this page fault is forced.
    pub(in vmar) fn is_forced(&self) -> bool {
        self.is_forced
    }

    /// Marks this page fault as forced.
    pub(super) fn force(mut self) -> Self {
        self.is_forced = true;
        self
    }
}
