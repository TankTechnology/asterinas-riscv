// SPDX-License-Identifier: MPL-2.0

use alloc::collections::BTreeMap;
use core::{
    alloc::Layout, hint::spin_loop, num::NonZeroUsize, ops::Range, ptr::NonNull, time::Duration,
};

use crab_usb::KernelOp;
use dma_api::{
    DmaAddr, DmaAllocHandle, DmaConstraints, DmaDirection, DmaError, DmaMapHandle, DmaOp,
};

use super::{DmaCoherent, DmaWindow};
use crate::{
    arch,
    io::IoMem,
    mm::{CachePolicy, HasPaddr, PAGE_SIZE, PageFlags, io::VmIoOnce},
    sync::SpinLock,
};

const EIC7700_UNCACHED_ALIAS_START: usize = 0xc0_0000_0000;
const EIC7700_UNCACHED_ALIAS_END: usize = 0xc4_0000_0000;
const EIC7700_CACHED_DRAM_START: usize = 0x8000_0000;
const EIC7700_CACHED_DRAM_SIZE: usize = 0x4_0000_0000;
const EIC7700_DMA_CPU_START: usize = 0xc000_0000;

#[derive(Debug)]
struct UsbDmaMemory {
    backing: DmaCoherent,
    uncached_alias: Option<IoMem>,
    dma_addr: u64,
    requested_size: usize,
}

impl UsbDmaMemory {
    fn cpu_addr(&self) -> NonNull<u8> {
        self.uncached_alias
            .as_ref()
            .map_or_else(|| self.backing.as_non_null_ptr(), IoMem::as_non_null_ptr)
    }
}

/// The OSTD services required by CrabUSB on a non-coherent platform.
pub struct UsbKernelOp {
    window: DmaWindow,
    allocations: SpinLock<BTreeMap<usize, UsbDmaMemory>>,
    mappings: SpinLock<BTreeMap<usize, UsbDmaMemory>>,
}

impl UsbKernelOp {
    /// Creates an adapter for a firmware-described DMA address window.
    pub fn new(window: DmaWindow) -> Self {
        Self {
            window,
            allocations: SpinLock::new(BTreeMap::new()),
            mappings: SpinLock::new(BTreeMap::new()),
        }
    }

    fn allocate_memory(
        &self,
        constraints: DmaConstraints,
        layout: Layout,
    ) -> Option<(UsbDmaMemory, NonNull<u8>, DmaAddr)> {
        if layout.size() == 0 {
            return None;
        }

        let nframes = layout.size().div_ceil(PAGE_SIZE);
        let backing = DmaCoherent::alloc(nframes, false).ok()?;
        let paddr = backing.paddr();
        let end = paddr.checked_add(layout.size())?;
        let device_range = self.window.translate(paddr..end)?;
        if !satisfies_constraints(device_range.start, layout.size(), constraints) {
            return None;
        }
        let dma_addr = DmaAddr::from(u64::try_from(device_range.start).ok()?);

        let uncached_alias = self.map_uncached_alias(&backing, nframes)?;
        let memory = UsbDmaMemory {
            backing,
            uncached_alias,
            dma_addr: dma_addr.as_u64(),
            requested_size: layout.size(),
        };
        let cpu_addr = memory.cpu_addr();
        if !(cpu_addr.as_ptr() as usize).is_multiple_of(layout.align()) {
            return None;
        }

        Some((memory, cpu_addr, dma_addr))
    }

    fn map_uncached_alias(&self, backing: &DmaCoherent, nframes: usize) -> Option<Option<IoMem>> {
        if !arch::mm::has_uncached_dram_alias() {
            return Some(None);
        }

        let paddr = backing.paddr();
        let size = nframes.checked_mul(PAGE_SIZE)?;
        let end = paddr.checked_add(size)?;
        let alias_range = eic7700_uncached_alias_range(&self.window, paddr..end)?;

        let cached_start = backing.as_non_null_ptr().as_ptr() as usize;
        let cached_end = cached_start.checked_add(size)?;
        arch::mm::sync_io_mem_to_device(
            paddr..end,
            cached_start..cached_end,
            CachePolicy::Writeback,
        )
        .ok()?;

        // SAFETY:
        //  - EIC7700 exposes this PMP-authorized physical range as a non-cacheable alias of DRAM;
        //  - `backing` owns the corresponding normal DRAM frames for the mapping lifetime;
        //  - USB DMA buffers are untyped memory, so accesses through this insensitive mapping
        //    cannot violate Rust's type invariants.
        Some(Some(unsafe {
            IoMem::new(alias_range, PageFlags::RW, CachePolicy::Uncacheable)
        }))
    }

    fn alloc(&self, constraints: DmaConstraints, layout: Layout) -> Option<DmaAllocHandle> {
        let (memory, cpu_addr, dma_addr) = self.allocate_memory(constraints, layout)?;
        let key = cpu_addr.as_ptr() as usize;
        let old = self.allocations.lock().insert(key, memory);
        assert!(old.is_none(), "DMA allocator returned a live address twice");

        // SAFETY: `cpu_addr` points into the allocation retained in `allocations`, and the
        // checked DMA window translation remains valid until `dealloc` removes that allocation.
        Some(unsafe { DmaAllocHandle::new(cpu_addr, dma_addr, layout) })
    }

    fn dealloc(&self, handle: DmaAllocHandle) {
        let key = handle.as_ptr().as_ptr() as usize;
        let memory = self.allocations.lock().remove(&key);
        assert!(memory.is_some(), "unknown DMA allocation");
        drop(memory);
    }

    #[cfg(ktest)]
    pub(super) fn allocation_count(&self) -> usize {
        self.allocations.lock().len()
    }

    pub(crate) fn log_dma_snapshot(&self) {
        let allocations = self.allocations.lock();
        let mappings = self.mappings.lock();
        crate::warn!(
            "USB DMA snapshot: allocations={}, mappings={}",
            allocations.len(),
            mappings.len()
        );
        for (index, memory) in allocations.values().enumerate() {
            log_memory_snapshot("allocation", index, memory);
        }
        for (index, memory) in mappings.values().enumerate() {
            log_memory_snapshot("mapping", index, memory);
        }
    }
}

impl DmaOp for UsbKernelOp {
    fn page_size(&self) -> usize {
        PAGE_SIZE
    }

    unsafe fn alloc_contiguous(
        &self,
        constraints: DmaConstraints,
        layout: Layout,
    ) -> Option<DmaAllocHandle> {
        self.alloc(constraints, layout)
    }

    unsafe fn dealloc_contiguous(&self, handle: DmaAllocHandle) {
        self.dealloc(handle);
    }

    unsafe fn alloc_coherent(
        &self,
        constraints: DmaConstraints,
        layout: Layout,
    ) -> Option<DmaAllocHandle> {
        self.alloc(constraints, layout)
    }

    unsafe fn dealloc_coherent(&self, handle: DmaAllocHandle) {
        self.dealloc(handle);
    }

    unsafe fn map_streaming(
        &self,
        constraints: DmaConstraints,
        addr: NonNull<u8>,
        size: NonZeroUsize,
        _direction: DmaDirection,
    ) -> Result<DmaMapHandle, DmaError> {
        let layout = Layout::from_size_align(size.get(), constraints.align.max(1))?;
        let (memory, bounce_addr, dma_addr) = self
            .allocate_memory(constraints, layout)
            .ok_or(DmaError::NoMemory)?;
        let key = bounce_addr.as_ptr() as usize;
        let old = self.mappings.lock().insert(key, memory);
        assert!(old.is_none(), "DMA allocator returned a live address twice");

        // SAFETY: The caller owns `addr` for the mapping lifetime, while `bounce_addr` points
        // into the allocation retained in `mappings` until `unmap_streaming`.
        Ok(unsafe { DmaMapHandle::new(addr, dma_addr, layout, Some(bounce_addr)) })
    }

    unsafe fn unmap_streaming(&self, handle: DmaMapHandle) {
        let key = handle
            .bounce_ptr()
            .expect("USB streaming mappings always use a bounce buffer")
            .as_ptr() as usize;
        let memory = self.mappings.lock().remove(&key);
        assert!(memory.is_some(), "unknown streaming DMA mapping");
        drop(memory);
    }
}

impl KernelOp for UsbKernelOp {
    fn delay(&self, duration: Duration) {
        let ticks = duration
            .as_nanos()
            .saturating_mul(arch::tsc_freq() as u128)
            .div_ceil(1_000_000_000)
            .min(u64::MAX as u128) as u64;
        let start = arch::read_tsc();
        while arch::read_tsc().wrapping_sub(start) < ticks {
            spin_loop();
        }
    }
}

fn log_memory_snapshot(kind: &str, index: usize, memory: &UsbDmaMemory) {
    let read_word = |offset| {
        memory.uncached_alias.as_ref().map_or_else(
            || memory.backing.read_once::<u64>(offset),
            |alias| alias.read_once::<u64>(offset),
        )
    };
    let head = [read_word(0).ok(), read_word(size_of::<u64>()).ok()];
    crate::warn!(
        "USB DMA {kind}[{index}]: paddr={:#x}, dma={:#x}, cpu={:#x}, alias={:?}, size={:#x}, head={head:x?}",
        memory.backing.paddr(),
        memory.dma_addr,
        memory.cpu_addr().as_ptr() as usize,
        memory.uncached_alias.as_ref().map(HasPaddr::paddr),
        memory.requested_size,
    );
}

fn satisfies_constraints(addr: usize, size: usize, constraints: DmaConstraints) -> bool {
    let Ok(start) = u64::try_from(addr) else {
        return false;
    };
    let Some(end) = start.checked_add(size.saturating_sub(1) as u64) else {
        return false;
    };

    if end > constraints.addr_mask || !addr.is_multiple_of(constraints.align.max(1)) {
        return false;
    }
    if constraints
        .max_segment_size
        .is_some_and(|maximum| size > maximum)
    {
        return false;
    }
    if let Some(boundary) = constraints.boundary.map(|value| value.max(1))
        && start / boundary as u64 != end / boundary as u64
    {
        return false;
    }

    true
}

pub(super) fn eic7700_uncached_alias_range(
    window: &DmaWindow,
    cpu_range: Range<usize>,
) -> Option<Range<usize>> {
    let is_parent_window =
        window.device_start() == 0 && window.cpu_start() == EIC7700_DMA_CPU_START;
    let is_direct_dram_window = window.device_start() == EIC7700_CACHED_DRAM_START
        && window.cpu_start() == EIC7700_CACHED_DRAM_START
        && window.size() == EIC7700_CACHED_DRAM_SIZE;
    if (!is_parent_window && !is_direct_dram_window)
        || cpu_range.start >= cpu_range.end
        || cpu_range.start < EIC7700_CACHED_DRAM_START
    {
        return None;
    }
    window.translate(cpu_range.clone())?;

    // EIC7700's memory-port map pairs cached DRAM at `0x8000_0000` with the
    // non-cacheable System Port 1 alias at `0xc0_0000_0000`. See OpenSBI
    // commit a0ec1bd63da409b727627b3f758e2fc7a6c685d0, `eic7700_mem_aliases`.
    let start_offset = cpu_range.start.checked_sub(EIC7700_CACHED_DRAM_START)?;
    let end_offset = cpu_range.end.checked_sub(EIC7700_CACHED_DRAM_START)?;
    let start = EIC7700_UNCACHED_ALIAS_START.checked_add(start_offset)?;
    let end = EIC7700_UNCACHED_ALIAS_START.checked_add(end_offset)?;
    (end <= EIC7700_UNCACHED_ALIAS_END).then_some(start..end)
}
