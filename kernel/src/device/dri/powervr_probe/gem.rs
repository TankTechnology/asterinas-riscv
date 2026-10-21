// SPDX-License-Identifier: MPL-2.0

//! Minimal PVR GEM objects for the DRM render node.
//!
//! A GEM object is a range of the DRI pool's contiguous physical memory.  The
//! render-node create ioctl maps that range into a dedicated job MMU context
//! and returns a handle plus the pool offset used by `mmap`; closing the
//! handle unmaps the range again.  The context root is prepared here but not
//! installed in the GPU catalogue yet; context activation is a later B3/B4
//! step.

use alloc::collections::BTreeMap;

use align_ext::AlignExt;
use ostd::{mm::PAGE_SIZE, sync::Mutex};
use spin::Once;

use super::mmu::GpuMmu4;

const GEM_VA_BASE: usize = 0x40_0000_0000;
const GEM_VA_LIMIT: usize = 1 << 40;
/// The DRI pool is 64 MiB; the same ceiling applies to one object.
const MAX_GEM_BYTES: usize = 64 * 1024 * 1024;

pub(super) struct PvrGemContext {
    mmu: GpuMmu4,
    next_vaddr: usize,
    mappings: BTreeMap<usize, usize>,
}

impl PvrGemContext {
    pub(super) fn new() -> Result<Self, &'static str> {
        Ok(Self {
            mmu: GpuMmu4::new()?,
            next_vaddr: GEM_VA_BASE,
            mappings: BTreeMap::new(),
        })
    }

    /// Map `size` bytes at contiguous physical address `paddr`.
    pub(super) fn create(&mut self, paddr: usize, size: usize) -> Result<usize, &'static str> {
        if size == 0 || size > MAX_GEM_BYTES {
            return Err("gpu_gem_invalid_size");
        }
        let pages = size.div_ceil(PAGE_SIZE);
        let bytes = pages
            .checked_mul(PAGE_SIZE)
            .ok_or("gpu_gem_context_overflow")?;
        let vaddr = self.next_vaddr.align_up(PAGE_SIZE);
        let end = vaddr.checked_add(bytes).ok_or("gpu_gem_context_overflow")?;
        if end > GEM_VA_LIMIT {
            return Err("gpu_gem_context_overflow");
        }
        self.mmu.map_external(vaddr, paddr, pages, false, true)?;
        self.next_vaddr = end;
        self.mappings.insert(vaddr, pages);
        Ok(vaddr)
    }

    pub(super) fn destroy(&mut self, vaddr: usize) -> Result<(), &'static str> {
        if !self.mappings.contains_key(&vaddr) {
            return Err("gpu_gem_unknown_mapping");
        }
        self.mmu.unmap_external(vaddr)?;
        self.mappings.remove(&vaddr);
        Ok(())
    }

    pub(super) fn count(&self) -> usize {
        self.mappings.len()
    }

    #[cfg(ktest)]
    pub(super) fn test_pte(&self, vaddr: usize) -> Result<u64, &'static str> {
        self.mmu.test_pte(vaddr)
    }
}

static GEM_CONTEXT: Once<Result<Mutex<PvrGemContext>, &'static str>> = Once::new();

fn context() -> Result<&'static Mutex<PvrGemContext>, &'static str> {
    GEM_CONTEXT
        .call_once(|| PvrGemContext::new().map(Mutex::new))
        .as_ref()
        .map_err(|reason| *reason)
}

/// Map one DRI pool range for a render-node GEM object.
pub(crate) fn create(paddr: usize, size: usize) -> Result<usize, &'static str> {
    context()?.lock().create(paddr, size)
}

/// Unmap a render-node GEM object by its GPU VA.
pub(crate) fn destroy(vaddr: usize) -> Result<(), &'static str> {
    context()?.lock().destroy(vaddr)
}

pub(crate) fn gems() -> usize {
    GEM_CONTEXT
        .get()
        .and_then(|result| result.as_ref().ok())
        .map(|ctx| ctx.lock().count())
        .unwrap_or(0)
}

#[cfg(ktest)]
mod tests {
    use ostd::{mm::PAGE_SIZE, prelude::ktest};

    use super::{GEM_VA_BASE, PvrGemContext};
    use crate::device::dri::powervr_probe::dma::GpuDmaAllocation;

    #[ktest]
    fn gpu_gem_create_maps_and_unmaps_a_contiguous_range() {
        let allocation = GpuDmaAllocation::new(1).unwrap();
        let mut ctx = PvrGemContext::new().unwrap();
        let vaddr = ctx.create(allocation.daddr(), PAGE_SIZE).unwrap();
        assert_eq!(vaddr, GEM_VA_BASE);
        assert_eq!(ctx.count(), 1);
        let pte = ctx.test_pte(vaddr).unwrap();
        assert_eq!(pte & 1, 1);
        assert_eq!(ctx.test_pte(vaddr + PAGE_SIZE), Ok(0));

        ctx.destroy(vaddr).unwrap();
        assert_eq!(ctx.count(), 0);
        assert_eq!(ctx.test_pte(vaddr), Ok(0));
        assert_eq!(ctx.destroy(vaddr), Err("gpu_gem_unknown_mapping"));
    }

    #[ktest]
    fn gpu_gem_create_keeps_distinct_context_ranges() {
        let first = GpuDmaAllocation::new(1).unwrap();
        let second = GpuDmaAllocation::new(1).unwrap();
        let mut ctx = PvrGemContext::new().unwrap();
        let first_va = ctx.create(first.daddr(), PAGE_SIZE).unwrap();
        let second_va = ctx.create(second.daddr(), PAGE_SIZE).unwrap();
        assert_eq!(second_va, first_va + PAGE_SIZE);
        assert_eq!(ctx.count(), 2);
        assert_eq!(ctx.test_pte(first_va).unwrap() & 1, 1);
        assert_eq!(ctx.test_pte(second_va).unwrap() & 1, 1);

        ctx.destroy(first_va).unwrap();
        assert_eq!(ctx.test_pte(first_va), Ok(0));
        assert_eq!(ctx.test_pte(second_va).unwrap() & 1, 1);
        ctx.destroy(second_va).unwrap();
        assert_eq!(ctx.count(), 0);
    }

    #[ktest]
    fn gpu_gem_create_rejects_invalid_sizes() {
        let allocation = GpuDmaAllocation::new(1).unwrap();
        let mut ctx = PvrGemContext::new().unwrap();
        assert_eq!(
            ctx.create(allocation.daddr(), 0),
            Err("gpu_gem_invalid_size")
        );
        assert_eq!(
            ctx.create(allocation.daddr(), super::MAX_GEM_BYTES + 1),
            Err("gpu_gem_invalid_size")
        );
        assert_eq!(ctx.count(), 0);
    }
}
