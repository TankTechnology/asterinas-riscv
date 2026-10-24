// SPDX-License-Identifier: MPL-2.0

//! Segments whose frames need not be physically contiguous.

use super::{Segment, meta::AnyFrameMeta};
use crate::mm::{
    HasPaddr, HasSize, Infallible, Vaddr,
    io::{
        VmReader, VmWriter,
        util::{HasVmReaderWriter, VmReaderWriterIdentity},
    },
    kspace::{
        LINEAR_MAPPING_BASE_VADDR, LINEAR_MAPPING_VADDR_RANGE, kvirt_area::KVirtArea,
    },
    paddr_to_vaddr,
};

/// A range of frames that is contiguous in virtual memory but not necessarily
/// in physical memory.
///
/// The frames are either one physically contiguous [`Segment`] or several
/// smaller segments mapped into a kernel virtual area. In both cases the
/// object provides a single virtual base address and [`HasSize`] bytes of
/// readable/writable memory.
///
/// Unlike [`Segment`], this type intentionally does not implement
/// [`HasPaddr`]: a segmented allocation has no single physical base address.
/// Callers that need physical contiguity (DMA, contiguous VMOs, ...) must keep
/// using [`Segment`].
pub struct SegmentedSegment<M: AnyFrameMeta + ?Sized> {
    backing: Backing<M>,
    nbytes: usize,
}

enum Backing<M: AnyFrameMeta + ?Sized> {
    Contiguous(Segment<M>),
    Mapped(KVirtArea),
}

impl<M: AnyFrameMeta + ?Sized> SegmentedSegment<M> {
    /// Creates a segmented segment backed by one physically contiguous segment.
    pub(crate) fn contiguous(segment: Segment<M>) -> Self {
        let nbytes = segment.size();
        Self {
            backing: Backing::Contiguous(segment),
            nbytes,
        }
    }

    /// Creates a segmented segment backed by several segments mapped into
    /// `area`. The caller must have mapped exactly `nbytes` bytes into the
    /// area.
    pub(crate) fn mapped(area: KVirtArea, nbytes: usize) -> Self {
        debug_assert!(nbytes <= area.size());
        Self {
            backing: Backing::Mapped(area),
            nbytes,
        }
    }

    /// Returns the virtual base address of the allocation.
    pub fn vaddr(&self) -> Vaddr {
        match &self.backing {
            Backing::Contiguous(segment) => paddr_to_vaddr(segment.paddr()),
            Backing::Mapped(area) => area.range().start,
        }
    }

    /// Returns whether the frames are physically contiguous.
    pub fn is_contiguous(&self) -> bool {
        matches!(self.backing, Backing::Contiguous(_))
    }

    /// Returns the virtual base address without releasing the allocation.
    ///
    /// The caller takes ownership of the leaked mapping and must eventually
    /// call [`Self::reclaim_from_vaddr`] with the same address and size.
    pub fn leak_vaddr(self) -> Vaddr {
        let vaddr = self.vaddr();
        core::mem::forget(self);
        vaddr
    }
}

impl<M: AnyFrameMeta> SegmentedSegment<M> {
    /// Releases an allocation whose virtual address was leaked with
    /// [`Self::leak_vaddr`].
    ///
    /// # Safety
    ///
    /// The caller must own the allocation: `vaddr` must come from a
    /// `leak_vaddr` call on a segment of type `M`, `nbytes` must be its size,
    /// and it must not have been reclaimed before.
    pub(crate) unsafe fn reclaim_from_vaddr(vaddr: Vaddr, nbytes: usize) {
        if LINEAR_MAPPING_VADDR_RANGE.contains(&vaddr) {
            let paddr = vaddr - LINEAR_MAPPING_BASE_VADDR;
            // SAFETY: The caller guarantees ownership of the frames.
            drop(unsafe { Segment::<M>::from_raw(paddr..paddr + nbytes) });
        } else {
            // SAFETY: The caller guarantees ownership of the mapping.
            drop(unsafe { KVirtArea::from_mapped_range(vaddr..vaddr + nbytes) });
        }
    }
}

impl<M: AnyFrameMeta + ?Sized> HasSize for SegmentedSegment<M> {
    fn size(&self) -> usize {
        self.nbytes
    }
}

impl<M: AnyFrameMeta + ?Sized> HasVmReaderWriter for SegmentedSegment<M> {
    type Types = VmReaderWriterIdentity;

    fn reader(&self) -> VmReader<'_, Infallible> {
        let ptr = self.vaddr() as *const u8;
        // SAFETY:
        // - The memory range is owned by this segment and lives for `'_`.
        // - Using `VmReader` and `VmWriter` is the only way to access it.
        unsafe { VmReader::from_kernel_space(ptr, self.nbytes) }
    }

    fn writer(&self) -> VmWriter<'_, Infallible> {
        let ptr = self.vaddr() as *mut u8;
        // SAFETY:
        // - The memory range is owned by this segment and lives for `'_`.
        // - Using `VmReader` and `VmWriter` is the only way to access it.
        unsafe { VmWriter::from_kernel_space(ptr, self.nbytes) }
    }
}
