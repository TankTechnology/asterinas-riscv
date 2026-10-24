// SPDX-License-Identifier: MPL-2.0

//! The physical memory allocator.

use core::{
    alloc::Layout,
    ops::Range,
    panic::Location,
    sync::atomic::{AtomicUsize, Ordering},
};

use align_ext::AlignExt;

use super::{
    Frame, meta::AnyFrameMeta, segment::Segment, segmented::SegmentedSegment,
};
use crate::{
    boot::memory_region::MemoryRegionType,
    error::Error,
    impl_frame_meta_for,
    mm::{
        PAGE_SIZE,
        kspace::kvirt_area::KVirtArea,
        page_prop::{CachePolicy, PageFlags, PageProperty, PrivilegedPageFlags},
        paddr_to_vaddr,
    },
    prelude::*,
    util::ops::range_difference,
};

/// The page count at which the segmented fallback starts splitting.
///
/// Requests up to this many pages are always served by one contiguous segment
/// (or fail); only larger requests may be split.
const SEGMENTED_FALLBACK_MIN_PAGES: usize = 4;

/// The largest chunk (in pages) tried first when splitting a large request.
const SEGMENTED_CHUNK_PAGES: usize = 4;

/// How many times the segmented fallback has been used.
static SEGMENTED_FALLBACK_COUNT: AtomicUsize = AtomicUsize::new(0);

/// How many segment allocations have failed (before any fallback).
static SEGMENT_FAILURE_COUNT: AtomicUsize = AtomicUsize::new(0);

/// How many failed segment allocations are reported individually.
const MAX_SEGMENT_FAILURE_REPORTS: usize = 32;

/// Reports a failed segment allocation together with its caller.
///
/// The first few failures are reported individually; afterwards every 256th
/// failure is reported so the total can be estimated without flooding the
/// console.
fn report_segment_failure(nframes: usize, caller: &'static Location<'static>) {
    let count = SEGMENT_FAILURE_COUNT.fetch_add(1, Ordering::Relaxed) + 1;
    if count <= MAX_SEGMENT_FAILURE_REPORTS || count % 256 == 0 {
        warn!(
            "segment allocation failed #{}: pages={} caller={}",
            count, nframes, caller
        );
    }
}

/// How many segmented fallbacks are reported before the log is suppressed.
const MAX_SEGMENTED_FALLBACK_LOGS: usize = 8;

/// The maximum number of chunks a segmented fallback will allocate.
///
/// The chunks are collected before the mapping starts (so no frame allocation
/// happens while page-table locks are held). 64 four-page chunks cover 1 MiB,
/// which is well beyond the large requests seen under fragmentation.
const SEGMENTED_MAX_CHUNKS: usize = 64;

/// Options for allocating physical memory frames.
pub struct FrameAllocOptions {
    zeroed: bool,
    /// Forces [`Self::alloc_segmented_with`] to skip the contiguous attempt.
    /// Only used by tests to exercise the fallback path.
    #[cfg(ktest)]
    force_segmented: bool,
}

impl Default for FrameAllocOptions {
    fn default() -> Self {
        Self::new()
    }
}

impl FrameAllocOptions {
    /// Creates new options for allocating the specified number of frames.
    pub fn new() -> Self {
        Self {
            zeroed: true,
            #[cfg(ktest)]
            force_segmented: false,
        }
    }

    /// Sets whether the allocated frames should be initialized with zeros.
    ///
    /// If `zeroed` is `true`, the allocated frames are filled with zeros.
    /// If not, the allocated frames will contain sensitive data and the caller
    /// should clear them before sharing them with other components.
    ///
    /// By default, the frames are zero-initialized.
    pub fn zeroed(&mut self, zeroed: bool) -> &mut Self {
        self.zeroed = zeroed;
        self
    }

    /// Allocates a single untyped frame without metadata.
    pub fn alloc_frame(&self) -> Result<Frame<()>> {
        self.alloc_frame_with(())
    }

    /// Allocates a single frame with additional metadata.
    pub fn alloc_frame_with<M: AnyFrameMeta>(&self, metadata: M) -> Result<Frame<M>> {
        let single_layout = Layout::from_size_align(PAGE_SIZE, PAGE_SIZE).unwrap();
        let frame = get_global_frame_allocator()
            .alloc(single_layout)
            .map(|paddr| Frame::from_unused(paddr, metadata).unwrap())
            .ok_or(Error::NoMemory)?;

        if self.zeroed {
            let addr = paddr_to_vaddr(frame.paddr()) as *mut u8;
            // SAFETY: The newly allocated frame is guaranteed to be valid.
            unsafe { core::ptr::write_bytes(addr, 0, PAGE_SIZE) }
        }

        Ok(frame)
    }

    /// Allocates a contiguous range of untyped frames without metadata.
    #[track_caller]
    pub fn alloc_segment(&self, nframes: usize) -> Result<Segment<()>> {
        self.alloc_segment_with(nframes, |_| ())
    }

    /// Allocates a contiguous range of frames with additional metadata.
    ///
    /// The returned [`Segment`] contains at least one frame. The method returns
    /// an error if the number of frames is zero.
    #[track_caller]
    pub fn alloc_segment_with<M: AnyFrameMeta, F>(
        &self,
        nframes: usize,
        metadata_fn: F,
    ) -> Result<Segment<M>>
    where
        F: FnMut(Paddr) -> M,
    {
        self.alloc_segment_with_inner(nframes, metadata_fn, Location::caller())
    }

    fn alloc_segment_with_inner<M: AnyFrameMeta, F>(
        &self,
        nframes: usize,
        metadata_fn: F,
        caller: &'static Location<'static>,
    ) -> Result<Segment<M>>
    where
        F: FnMut(Paddr) -> M,
    {
        if nframes == 0 {
            return Err(Error::InvalidArgs);
        }
        let layout = Layout::from_size_align(nframes * PAGE_SIZE, PAGE_SIZE).unwrap();
        let segment = match get_global_frame_allocator().alloc(layout) {
            Some(start) => {
                Segment::from_unused(start..start + nframes * PAGE_SIZE, metadata_fn).unwrap()
            }
            None => {
                report_segment_failure(nframes, caller);
                return Err(Error::NoMemory);
            }
        };

        if self.zeroed {
            let addr = paddr_to_vaddr(segment.paddr()) as *mut u8;
            // SAFETY: The newly allocated segment is guaranteed to be valid.
            unsafe { core::ptr::write_bytes(addr, 0, nframes * PAGE_SIZE) }
        }

        Ok(segment)
    }

    /// Allocates a range of untyped frames that may be physically
    /// non-contiguous.
    ///
    /// See [`Self::alloc_segmented_with`] for the fallback behavior.
    #[track_caller]
    pub fn alloc_segmented(&self, nframes: usize) -> Result<SegmentedSegment<()>> {
        self.alloc_segmented_with(nframes, |_| ())
    }

    /// Allocates a range of frames that may be physically non-contiguous.
    ///
    /// The method first tries a single contiguous segment, exactly like
    /// [`Self::alloc_segment_with`]. If that fails and the request is larger
    /// than an order-2 block, it allocates several smaller chunks (starting at
    /// four pages and halving on failure) and maps them into one kernel
    /// virtual area. The caller sees one virtually contiguous buffer and no
    /// physical base address.
    ///
    /// A failure rolls back: every chunk allocated so far is released before
    /// `Error::NoMemory` is returned.
    #[track_caller]
    pub fn alloc_segmented_with<M: AnyFrameMeta, F>(
        &self,
        nframes: usize,
        mut metadata_fn: F,
    ) -> Result<SegmentedSegment<M>>
    where
        F: FnMut(Paddr) -> M,
    {
        let caller = Location::caller();
        if nframes == 0 {
            return Err(Error::InvalidArgs);
        }

        let try_contiguous = {
            #[cfg(ktest)]
            {
                !self.force_segmented
            }
            #[cfg(not(ktest))]
            {
                true
            }
        };

        if try_contiguous {
            match self.alloc_segment_with_inner(nframes, &mut metadata_fn, caller) {
                Ok(segment) => return Ok(SegmentedSegment::contiguous(segment)),
                Err(Error::NoMemory) if nframes > SEGMENTED_FALLBACK_MIN_PAGES => {}
                Err(err) => return Err(err),
            }
        }

        let nbytes = nframes * PAGE_SIZE;
        let prop = PageProperty {
            flags: PageFlags::RW,
            cache: CachePolicy::Writeback,
            priv_flags: PrivilegedPageFlags::empty(),
        };

        // Allocate every chunk before mapping anything. Mapping holds
        // page-table locks, and allocating frames under those locks risks a
        // lock-order inversion with the heap and the page-fault path.
        if nframes.div_ceil(SEGMENTED_CHUNK_PAGES) > SEGMENTED_MAX_CHUNKS {
            return Err(Error::NoMemory);
        }
        let mut segments: [Option<Segment<M>>; SEGMENTED_MAX_CHUNKS] =
            [const { None }; SEGMENTED_MAX_CHUNKS];
        let mut remaining = nframes;
        let mut used = 0;
        while remaining > 0 {
            let mut pages = SEGMENTED_CHUNK_PAGES.min(remaining);
            let segment = loop {
                let mut options = FrameAllocOptions::new();
                options.zeroed(self.zeroed);
                match options.alloc_segment_with_inner(pages, &mut metadata_fn, caller) {
                    Ok(segment) => break segment,
                    // Start with the largest chunk and halve it if the buddy
                    // allocator cannot provide it.
                    Err(_) if pages > 1 => pages /= 2,
                    // Dropping `segments` releases every chunk allocated so far.
                    Err(err) => return Err(err),
                }
            };
            remaining -= pages;
            segments[used] = Some(segment);
            used += 1;
        }

        let frames = segments
            .iter_mut()
            .take(used)
            .filter_map(|segment| segment.take())
            .flat_map(|segment| segment.into_iter());
        let area = KVirtArea::try_map_frames(nbytes, 0, frames, prop)?;

        let count = SEGMENTED_FALLBACK_COUNT.fetch_add(1, Ordering::Relaxed) + 1;
        if count <= MAX_SEGMENTED_FALLBACK_LOGS || count % 64 == 0 {
            warn!(
                "frame allocator: segmented fallback #{}: pages={} bytes={} caller={}",
                count, nframes, nbytes, caller
            );
        }

        Ok(SegmentedSegment::mapped(area, nbytes))
    }
}

#[cfg(ktest)]
#[ktest(expect_redundant_test_prefix)]
fn test_alloc_dealloc() {
    // Here we allocate and deallocate frames in random orders to test the allocator.
    // We expect the test to fail if the underlying implementation panics.
    let single_options = FrameAllocOptions::new();
    let mut contiguous_options = FrameAllocOptions::new();
    contiguous_options.zeroed(false);
    let mut remember_vec = Vec::new();
    for _ in 0..10 {
        for i in 0..10 {
            let single_frame = single_options.alloc_frame().unwrap();
            if i % 3 == 0 {
                remember_vec.push(single_frame);
            }
        }
        let contiguous_segment = contiguous_options.alloc_segment(10).unwrap();
        drop(contiguous_segment);
        remember_vec.pop();
    }
}

#[cfg(ktest)]
#[ktest(expect_redundant_test_prefix)]
fn test_alloc_segmented_fallback() {
    use crate::mm::{VmIo, VmReader, VmWriter};

    let mut options = FrameAllocOptions::new();
    options.zeroed(false);
    // Skip the contiguous attempt so the chunked path is always taken.
    options.force_segmented = true;

    // Five pages: one four-page chunk plus a one-page tail. A fragmented
    // allocator that can still serve order-2 chunks must satisfy this.
    let segment = options.alloc_segmented_with(5, |_| ()).unwrap();
    assert_eq!(segment.size(), 5 * PAGE_SIZE);
    assert!(!segment.is_contiguous());

    // The first and the last chunk must be reachable through the single
    // virtual mapping.
    let source = [0x5a_u8; 64];
    for offset in [0, 5 * PAGE_SIZE - 64] {
        let mut reader = VmReader::from(&source[..]).to_fallible();
        segment.write(offset, &mut reader).unwrap();

        let mut buf = [0u8; 64];
        let mut writer = VmWriter::from(&mut buf[..]).to_fallible();
        segment.read(offset, &mut writer).unwrap();
        assert_eq!(buf, source);
    }
}

/// The trait for the global frame allocator.
///
/// OSTD allows a customized frame allocator by the [`global_frame_allocator`]
/// attribute, which marks a static variable of this type.
///
/// The API mimics the standard Rust allocator API ([`GlobalAlloc`] and
/// [`global_allocator`]). However, this trait is much safer. Double free
/// or freeing in-use memory through this trait only messes up the allocator's
/// state rather than causing undefined behavior.
///
/// Whenever OSTD or other modules need to allocate or deallocate frames via
/// [`FrameAllocOptions`], they are forwarded to the global frame allocator.
/// It is not encouraged to call the global allocator directly.
///
/// [`global_frame_allocator`]: crate::global_frame_allocator
/// [`GlobalAlloc`]: core::alloc::GlobalAlloc
pub trait GlobalFrameAllocator: Sync {
    /// Allocates a contiguous range of frames.
    ///
    /// The caller guarantees that `layout.size()` is aligned to [`PAGE_SIZE`].
    ///
    /// When any of the allocated memory is not in use, OSTD returns them by
    /// calling [`GlobalFrameAllocator::dealloc`]. If multiple frames are
    /// allocated, they may be returned in any order with any number of calls.
    fn alloc(&self, layout: Layout) -> Option<Paddr>;

    /// Deallocates a contiguous range of frames.
    ///
    /// The caller guarantees that `addr` and `size` are both aligned to
    /// [`PAGE_SIZE`]. The deallocated memory should always be allocated by
    /// [`GlobalFrameAllocator::alloc`]. However, if
    /// [`GlobalFrameAllocator::alloc`] returns multiple frames, it is possible
    /// that some of them are deallocated before others. The deallocated memory
    /// must never overlap with any memory that is already deallocated or
    /// added, without being allocated in between.
    ///
    /// The deallocated memory can be uninitialized.
    fn dealloc(&self, addr: Paddr, size: usize);

    /// Adds a contiguous range of frames to the allocator.
    ///
    /// The memory being added must never overlap with any memory that was
    /// added before.
    ///
    /// The added memory can be uninitialized.
    fn add_free_memory(&self, addr: Paddr, size: usize);
}

unsafe extern "Rust" {
    /// The global frame allocator's reference exported by
    /// [`crate::global_frame_allocator`].
    static __GLOBAL_FRAME_ALLOCATOR_REF: &'static dyn GlobalFrameAllocator;
}

pub(super) fn get_global_frame_allocator() -> &'static dyn GlobalFrameAllocator {
    // SAFETY: The global frame allocator is set up correctly with the
    // `global_frame_allocator` attribute. If they use safe code only, the
    // up-call is safe.
    unsafe { __GLOBAL_FRAME_ALLOCATOR_REF }
}

/// Initializes the global frame allocator.
///
/// It just does adds the frames to the global frame allocator. Calling it
/// multiple times would be not safe.
///
/// # Safety
///
/// This function should be called only once.
pub(crate) unsafe fn init() {
    // Retire the early allocator.
    let early_allocator = EARLY_ALLOCATOR.lock().take().unwrap();
    let early_allocated_ranges = early_allocator.allocated_regions();

    #[cfg(all(target_arch = "x86_64", feature = "cvm_guest"))]
    let unaccepted_range = crate::if_tdx_enabled!({
        super::unaccepted::init_unaccepted_memory_state(&early_allocated_ranges)
    } else {
        0..0
    });
    #[cfg(not(all(target_arch = "x86_64", feature = "cvm_guest")))]
    let unaccepted_range: Range<Paddr> = 0..0;

    let regions = &crate::boot::EARLY_INFO.get().unwrap().memory_regions;
    let [range_1, range_2] = &early_allocated_ranges;

    for region in regions
        .iter()
        .filter(|region| region.typ() == MemoryRegionType::Usable)
    {
        debug_assert!(region.base().is_multiple_of(PAGE_SIZE));
        debug_assert!(region.len().is_multiple_of(PAGE_SIZE));

        let region_range = region.base()..region.end();
        for free_range in range_difference(&region_range, range_1)
            .flat_map(|range| range_difference(&range, range_2))
            .flat_map(|range| range_difference(&range, &unaccepted_range))
        {
            crate::info!("Adding free frames to the allocator: {:x?}", free_range);
            get_global_frame_allocator().add_free_memory(free_range.start, free_range.len());
        }
    }
}

/// An allocator in the early boot phase when frame metadata is not available.
pub(super) struct EarlyFrameAllocator {
    // We need to allocate from under 4G first since the linear mapping for
    // the higher region is not constructed yet.
    under_4g_range: Range<Paddr>,
    under_4g_end: Paddr,

    // And also sometimes 4G is not enough for early phase. This, if not `0..0`,
    // is the largest region above 4G.
    max_range: Range<Paddr>,
    max_end: Paddr,
}

/// The global frame allocator in the early boot phase.
///
/// It is used to allocate frames before the frame metadata is initialized.
/// The allocated frames are not tracked by the frame metadata. After the
/// metadata is initialized with [`super::meta::init`], the frames are tracked
/// with metadata and the early allocator is no longer used.
///
/// This is protected by the [`spin::Mutex`] rather than [`crate::sync::SpinLock`]
/// since the latter uses CPU-local storage, which isn't available in the early
/// boot phase. So we must make sure that no interrupts are enabled when using
/// this allocator.
pub(super) static EARLY_ALLOCATOR: spin::Mutex<Option<EarlyFrameAllocator>> =
    spin::Mutex::new(None);

impl EarlyFrameAllocator {
    /// Creates a new early frame allocator.
    ///
    /// It uses at most 2 regions, the first is the maximum usable region below
    /// 4 GiB. The other is the maximum usable region above 4 GiB and is only
    /// usable when linear mapping is constructed.
    pub(crate) fn new() -> Self {
        let regions = &crate::boot::EARLY_INFO.get().unwrap().memory_regions;

        let mut under_4g_range = 0..0;
        let mut max_range = 0..0;
        for region in regions.iter() {
            if region.typ() != MemoryRegionType::Usable {
                continue;
            }
            const PADDR4G: Paddr = 0x1_0000_0000;
            if region.base() < PADDR4G {
                let range = region.base()..region.end().min(PADDR4G);
                if range.len() > under_4g_range.len() {
                    under_4g_range = range;
                }
            }
            if region.end() >= PADDR4G {
                let range = region.base().max(PADDR4G)..region.end();
                if range.len() > max_range.len() {
                    max_range = range;
                }
            }
        }

        crate::debug!(
            "Early frame allocator (below 4G) at: {:#x?}",
            under_4g_range
        );
        if !max_range.is_empty() {
            crate::debug!("Early frame allocator (above 4G) at: {:#x?}", max_range);
        }

        Self {
            under_4g_range: under_4g_range.clone(),
            under_4g_end: under_4g_range.start,
            max_range: max_range.clone(),
            max_end: max_range.start,
        }
    }

    /// Allocates a contiguous range of frames.
    pub(super) fn alloc(&mut self, layout: Layout) -> Option<Paddr> {
        let size = layout.size().align_up(PAGE_SIZE);
        let align = layout.align().max(PAGE_SIZE);

        for (tail, end) in [
            (&mut self.under_4g_end, self.under_4g_range.end),
            (&mut self.max_end, self.max_range.end),
        ] {
            let allocated = tail.align_up(align);
            if let Some(allocated_end) = allocated.checked_add(size)
                && allocated_end <= end
            {
                *tail = allocated_end;
                return Some(allocated);
            }
        }

        None
    }

    pub(super) fn allocated_regions(&self) -> [Range<Paddr>; 2] {
        [
            self.under_4g_range.start..self.under_4g_end,
            self.max_range.start..self.max_end,
        ]
    }
}

/// Metadata for frames allocated in the early boot phase.
///
/// Frames allocated with [`early_alloc`] are not immediately tracked with
/// frame metadata. But [`super::meta::init`] will track them later.
#[derive(Debug)]
pub(crate) struct EarlyAllocatedFrameMeta;

impl_frame_meta_for!(EarlyAllocatedFrameMeta);

/// Allocates a contiguous range of frames in the early boot phase.
///
/// The early allocated frames will not be reclaimable, until the metadata is
/// initialized by [`super::meta::init`]. Then we can use [`Frame::from_raw`]
/// to free the frames.
///
/// # Panics
///
/// This function panics if:
///  - it is called before [`init_early_allocator`],
///  - or if is called after [`init`].
pub(crate) fn early_alloc(layout: Layout) -> Option<Paddr> {
    let mut early_allocator = EARLY_ALLOCATOR.lock();
    let paddr = early_allocator.as_mut().unwrap().alloc(layout)?;

    #[cfg(all(target_arch = "x86_64", feature = "cvm_guest"))]
    crate::if_tdx_enabled!({
        // SAFETY: The early allocator only exists in the boot context of the BSP before the APs
        // boot up. Therefore, there are no other concurrent operations that accept pages.
        unsafe { super::unaccepted::accept_early_allocated_range(paddr, layout.size()) };
    });

    Some(paddr)
}

/// Initializes the early frame allocator.
///
/// [`early_alloc`] should be used after this initialization. After [`init`], the
/// early allocator.
///
/// # Safety
///
/// This function should be called only once after the memory regions are ready.
pub(crate) unsafe fn init_early_allocator() {
    let mut early_allocator = EARLY_ALLOCATOR.lock();
    *early_allocator = Some(EarlyFrameAllocator::new());
}
