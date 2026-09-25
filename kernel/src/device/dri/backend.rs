// SPDX-License-Identifier: MPL-2.0

//! Device-specific presentation behind the DRM/KMS state machine.
//!
//! Everything above this module — dumb buffers, GEM objects, framebuffer
//! registration, the ioctl surface — is device-independent. The part that is
//! not is "make these pixels visible", and that is all [`ScanoutBackend`]
//! covers, with the optional [`CursorBackend`] covering the hardware cursor.
//!
//! Two implementations exist:
//!
//! * [`GpuDevice`], which hands the buffer to a virtio-gpu host over the 2D
//!   command stream;
//! * [`FirmwareFramebufferBackend`], which copies the pixels into the mode and
//!   memory a bootloader left active. It programs no display hardware at all —
//!   it exists so that a board whose only display is the firmware's framebuffer
//!   still gets a working KMS, and so that a future native backend can replace
//!   it without touching anything above this file.
//!
//! # Why the buffer carries a [`Vmo`] rather than an address
//!
//! The virtio-gpu path only ever needs the guest-physical address of the pixels.
//! The firmware path needs the **pixels**, because it copies them. Handing the
//! backend a raw address would therefore have made the second implementation
//! impossible without also teaching it to map physical memory. Passing the
//! `Vmo` and letting each backend derive what it needs costs the virtio path one
//! `paddr()` call and keeps the copy path in terms of `VmIo`.

use core::time::Duration;

use aster_framebuffer::{framebuffer::FrameBuffer, pixel::PixelFormat};
use aster_virtio::device::gpu::device::GpuDevice;
use ostd::mm::HasSize;

use super::cursor::{CursorPosition, MAX_CURSOR_SIZE};
use crate::{prelude::*, vm::page_cache::Vmo};

/// Bytes per pixel of the `BgrReserved8888` format the firmware path requires.
const BGRX8888_BYTES_PER_PIXEL: usize = 4;
const PRESENT_REPORT_INTERVAL: Duration = Duration::from_secs(5);

/// A framebuffer ready for presentation.
///
/// The `Vmo` is the device-wide dumb-buffer pool and `source_offset_bytes` is
/// the object's offset within it, so the two together name one GEM object's
/// pixels without copying them.
pub(super) struct ScanoutBuffer {
    source: Arc<Vmo>,
    source_offset_bytes: usize,
    pitch_bytes: usize,
    size_bytes: u32,
    width: u32,
    height: u32,
}

impl ScanoutBuffer {
    pub(super) fn new(
        source: Arc<Vmo>,
        source_offset_bytes: usize,
        pitch_bytes: usize,
        size_bytes: u32,
        width: u32,
        height: u32,
    ) -> Self {
        Self {
            source,
            source_offset_bytes,
            pitch_bytes,
            size_bytes,
            width,
            height,
        }
    }

    pub(super) fn dimensions(&self) -> (u32, u32) {
        (self.width, self.height)
    }

    /// The guest-physical address the pixels start at.
    ///
    /// Only meaningful for a backend that hands the address to something else;
    /// the pool is allocated contiguous, so a failure here means the pool was
    /// not set up as this module assumes.
    fn paddr(&self) -> Result<u64> {
        self.source
            .paddr()
            .and_then(|base| base.checked_add(self.source_offset_bytes))
            .map(|address| address as u64)
            .ok_or_else(|| Error::with_message(Errno::ENOMEM, "scanout backing is not contiguous"))
    }
}

/// A cursor image and position ready for presentation.
pub(super) struct CursorScanoutBuffer {
    source: Arc<Vmo>,
    source_offset_bytes: usize,
    size_bytes: u32,
    width: u32,
    height: u32,
    hot_x: u32,
    hot_y: u32,
    x: i32,
    y: i32,
}

/// The image geometry of one cursor update, without its backing store.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct CursorGeometry {
    pub(super) width: u32,
    pub(super) height: u32,
    pub(super) hot_x: u32,
    pub(super) hot_y: u32,
}

impl CursorScanoutBuffer {
    pub(super) fn new(
        source: Arc<Vmo>,
        source_offset_bytes: usize,
        size_bytes: u32,
        geometry: CursorGeometry,
        position: CursorPosition,
    ) -> Self {
        Self {
            source,
            source_offset_bytes,
            size_bytes,
            width: geometry.width,
            height: geometry.height,
            hot_x: geometry.hot_x,
            hot_y: geometry.hot_y,
            x: position.x,
            y: position.y,
        }
    }
}

/// A rectangle of a framebuffer that changed, in pixels.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct DamageRect {
    x1: u32,
    y1: u32,
    x2: u32,
    y2: u32,
}

impl DamageRect {
    /// Validates a half-open rectangle against the scanout it addresses.
    pub(super) fn new(x1: u32, y1: u32, x2: u32, y2: u32, width: u32, height: u32) -> Result<Self> {
        if x1 >= x2 || y1 >= y2 || x2 > width || y2 > height {
            return_errno_with_message!(Errno::EINVAL, "damage is outside the scanout");
        }
        Ok(Self { x1, y1, x2, y2 })
    }

    fn copied_bytes(&self) -> u64 {
        u64::from(self.x2 - self.x1)
            .saturating_mul(u64::from(self.y2 - self.y1))
            .saturating_mul(BGRX8888_BYTES_PER_PIXEL as u64)
    }
}

#[derive(Clone, Copy)]
enum PresentKind {
    Full,
    Dirty,
}

#[derive(Clone, Copy, Default)]
struct PresentClassStats {
    count: u64,
    bytes: u64,
    total_ns: u64,
    max_ns: u64,
}

#[derive(Clone, Copy, Default)]
struct FirmwarePresentStats {
    full: PresentClassStats,
    dirty: PresentClassStats,
    total_successes: u64,
    last_reported_at: Option<Duration>,
}

impl FirmwarePresentStats {
    /// Returns a snapshot at exponential milestones and after a bounded interval.
    fn record_success(
        &mut self,
        kind: PresentKind,
        bytes: u64,
        elapsed_ns: u64,
        finished_at: Duration,
    ) -> Option<Self> {
        let class = match kind {
            PresentKind::Full => &mut self.full,
            PresentKind::Dirty => &mut self.dirty,
        };
        class.count = class.count.saturating_add(1);
        class.bytes = class.bytes.saturating_add(bytes);
        class.total_ns = class.total_ns.saturating_add(elapsed_ns);
        class.max_ns = class.max_ns.max(elapsed_ns);
        self.total_successes = self.total_successes.saturating_add(1);
        let interval_elapsed = self
            .last_reported_at
            .is_none_or(|last| finished_at.saturating_sub(last) >= PRESENT_REPORT_INTERVAL);
        if self.total_successes.is_power_of_two() || interval_elapsed {
            self.last_reported_at = Some(finished_at);
            Some(*self)
        } else {
            None
        }
    }
}

/// The operations the generic KMS state needs from a display device.
///
/// Deliberately small. There is no `disable_scanout`: no code path disables a
/// scanout today — `MODE_SETCRTC` with `fb_id == 0` returns success and leaves
/// the current image up — so a method for it would be a contract with no
/// caller. It is the first method to add when blanking is implemented, and
/// [`FirmwareFramebufferBackend`] is the backend that would most obviously
/// want it.
pub(super) trait ScanoutBackend: Send + Sync {
    /// Returns the active scanout dimensions in pixels.
    fn dimensions(&self) -> (u32, u32);

    /// The one mode this scanout is fixed at, when it cannot be changed.
    ///
    /// A backend that programs the display can accept many modes, and reports
    /// `None` — the range it will take is a property of the device, not of
    /// this trait. A backend that only copies into memory firmware already
    /// programmed has exactly one mode, and reporting the range a programmable
    /// display allows would invite a client to set a mode that
    /// [`Self::present_framebuffer`] then refuses.
    fn fixed_mode(&self) -> Option<(u32, u32)> {
        None
    }

    /// Presents one linear framebuffer on the active scanout.
    fn present_framebuffer(&self, buffer: ScanoutBuffer) -> Result<()>;

    /// Re-presents the active framebuffer after the given regions changed.
    ///
    /// Backends that cannot update incrementally keep the default, which
    /// re-presents the whole framebuffer. The distinction exists because the
    /// firmware path copies pixels with the CPU, where re-copying a whole
    /// screen to change a cursor is worth avoiding; the virtio path pushes the
    /// whole transfer either way, because that is what the host command does.
    fn dirty_framebuffer(&self, buffer: ScanoutBuffer, _damage: &[DamageRect]) -> Result<()> {
        self.present_framebuffer(buffer)
    }
}

/// Hardware-cursor operations, for backends that have a hardware cursor.
///
/// Kept separate from [`ScanoutBackend`] and held as an `Option` by the driver
/// because not every backend can have one: [`FirmwareFramebufferBackend`] owns
/// no display hardware, so it cannot place a cursor at all. A backend that
/// cannot cursor must not be forced to pretend it can.
pub(super) trait CursorBackend: Send + Sync {
    /// Returns the maximum hardware-cursor dimensions.
    fn dimensions(&self) -> (u32, u32);

    /// Replaces the hardware cursor image and position, returning the resource
    /// id now in use, which the caller records to hide it later.
    fn update_cursor(&self, buffer: CursorScanoutBuffer) -> Result<u32>;

    /// Moves the active hardware cursor without replacing its image.
    fn move_cursor(&self, x: i32, y: i32) -> Result<()>;

    /// Hides the hardware cursor.
    fn hide_cursor(&self, x: i32, y: i32) -> Result<()>;

    /// Hides the cursor only when `resource_id` is still the active one.
    ///
    /// A closing file owns a cursor image it installed, but another file may
    /// have installed a newer one since; this returns whether it still had to
    /// act, so closing the first file cannot hide the second's cursor.
    fn clear_cursor(&self, resource_id: u32, x: i32, y: i32) -> Result<bool>;
}

// The virtio-gpu implementation is a thin adapter: the device already does the
// work, and this only supplies the address its commands need. It is
// deliberately a straight translation with no policy, because the desktop this
// tree already renders depends on the behaviour underneath being unchanged.

impl ScanoutBackend for GpuDevice {
    fn dimensions(&self) -> (u32, u32) {
        (GpuDevice::width(self), GpuDevice::height(self))
    }

    fn present_framebuffer(&self, buffer: ScanoutBuffer) -> Result<()> {
        let addr = buffer.paddr()?;
        GpuDevice::present_framebuffer(self, addr, buffer.size_bytes, buffer.width, buffer.height)
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu present failed"))
    }
}

impl CursorBackend for GpuDevice {
    fn dimensions(&self) -> (u32, u32) {
        (MAX_CURSOR_SIZE, MAX_CURSOR_SIZE)
    }

    fn update_cursor(&self, buffer: CursorScanoutBuffer) -> Result<u32> {
        let addr = {
            let (source, offset) = (&buffer.source, buffer.source_offset_bytes);
            source
                .paddr()
                .and_then(|base| base.checked_add(offset))
                .map(|address| address as u64)
                .ok_or_else(|| {
                    Error::with_message(Errno::ENOMEM, "cursor backing is not contiguous")
                })?
        };
        GpuDevice::update_cursor(
            self,
            addr,
            buffer.size_bytes,
            buffer.width,
            buffer.height,
            buffer.hot_x,
            buffer.hot_y,
            buffer.x,
            buffer.y,
        )
        .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu cursor update failed"))
    }

    fn move_cursor(&self, x: i32, y: i32) -> Result<()> {
        GpuDevice::move_cursor(self, x, y)
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu cursor move failed"))
    }

    fn hide_cursor(&self, x: i32, y: i32) -> Result<()> {
        GpuDevice::hide_cursor(self, x, y)
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu cursor hide failed"))
    }

    fn clear_cursor(&self, resource_id: u32, x: i32, y: i32) -> Result<bool> {
        GpuDevice::clear_cursor(self, resource_id, x, y)
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu cursor clear failed"))
    }
}

/// A backend that copies DRM buffers into the framebuffer a bootloader left active.
///
/// This is the path for a board that boots with a display already programmed:
/// the firmware chose the mode, owns the HDMI link, and left a region of memory
/// the display reads continuously. Presenting is therefore a memory copy into
/// that region — no mode set, no page flip, no display registers.
///
/// # What this backend does not do
///
/// It does not program the display controller, so it cannot change the mode:
/// [`Self::dimensions`] is whatever firmware chose, fixed for the lifetime of
/// the device. It does not composite a cursor, so the driver reports no
/// [`CursorBackend`]. It does not detect hotplug, and it cannot power the link
/// down. Those are the job of a native backend for the specific controller, and
/// the point of the split is that writing one does not disturb anything above
/// this file.
///
/// Until then, this is enough for a KMS client to put a picture on a physical
/// board — which is the whole of the claim it makes.
pub(super) struct FirmwareFramebufferBackend {
    framebuffer: Arc<FrameBuffer>,
    width: u32,
    height: u32,
    /// Bytes per row of a DRM buffer this backend accepts, which is the width
    /// in pixels times four — not the framebuffer's own stride, which may be
    /// larger and is padded to by [`Self::present_framebuffer`].
    row_bytes: usize,
    /// One row of scratch space, so a copy never needs a second full buffer.
    scratch_row: Mutex<Vec<u8>>,
    present_stats: Mutex<FirmwarePresentStats>,
}

impl FirmwareFramebufferBackend {
    /// Whether this framebuffer's layout is one the backend can present through.
    ///
    /// Separate from [`Self::new`] so that the driver can ask the question
    /// without paying for the backend — it decides whether to expose a DRM node
    /// at all — and both read the same check, so the answer cannot differ from
    /// what construction would do.
    pub(super) fn accepts(framebuffer: &FrameBuffer) -> bool {
        firmware_layout(framebuffer).is_ok()
    }

    /// Wraps the registered framebuffer, refusing a layout it cannot copy into.
    pub(super) fn new(framebuffer: Arc<FrameBuffer>) -> Result<Self> {
        let (width, height, row_bytes) = firmware_layout(&framebuffer)?;

        Ok(Self {
            framebuffer,
            width,
            height,
            row_bytes,
            scratch_row: Mutex::new(vec![0; row_bytes]),
            present_stats: Mutex::new(FirmwarePresentStats::default()),
        })
    }

    fn record_present(
        &self,
        kind: PresentKind,
        bytes: u64,
        started: Duration,
        finished: Duration,
    ) -> Option<FirmwarePresentStats> {
        let elapsed = finished.saturating_sub(started);
        let elapsed_ns = u64::try_from(elapsed.as_nanos()).unwrap_or(u64::MAX);
        self.present_stats
            .lock()
            .record_success(kind, bytes, elapsed_ns, finished)
    }

    fn log_present(report: FirmwarePresentStats) {
        ostd::info!(
            "ASTERINAS_DRM_SCANOUT at_ns={} successes={} full_count={} full_bytes={} full_total_ns={} full_max_ns={} dirty_count={} dirty_bytes={} dirty_total_ns={} dirty_max_ns={}",
            report.last_reported_at.unwrap_or_default().as_nanos(),
            report.total_successes,
            report.full.count,
            report.full.bytes,
            report.full.total_ns,
            report.full.max_ns,
            report.dirty.count,
            report.dirty.bytes,
            report.dirty.total_ns,
            report.dirty.max_ns,
        );
    }

    /// Refuses a buffer whose geometry the firmware scanout cannot accept.
    fn validate_buffer(&self, buffer: &ScanoutBuffer) -> Result<()> {
        // The mode is fixed, so a buffer of another size has nowhere to go.
        // Refusing is the honest answer: accepting it would either scale
        // (which this backend does not do) or write a partial frame.
        if buffer.dimensions() != self.dimensions() || buffer.pitch_bytes != self.row_bytes {
            return_errno_with_message!(
                Errno::EINVAL,
                "scanout buffer does not match the fixed firmware mode"
            );
        }
        let required_size = buffer
            .pitch_bytes
            .checked_mul(buffer.height as usize)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "scanout size overflows"))?;
        if required_size > buffer.size_bytes as usize {
            return_errno_with_message!(Errno::EINVAL, "scanout buffer is truncated");
        }
        Ok(())
    }
}

/// Reads a registered framebuffer's own layout and validates it.
fn firmware_layout(framebuffer: &FrameBuffer) -> Result<(u32, u32, usize)> {
    validate_firmware_layout(
        framebuffer.width(),
        framebuffer.height(),
        framebuffer.line_size(),
        framebuffer.io_mem().size(),
        framebuffer.pixel_format(),
    )
}

/// Validates the layout firmware handed us, returning `(width, height, row_bytes)`.
///
/// Checks the properties a copy loop would otherwise discover by writing
/// outside the mapping or reading outside the buffer: a non-empty mode, a
/// packing this backend can interpret, a stride wide enough for a row, and a
/// mapping that covers the last pixel the mode addresses.
fn validate_firmware_layout(
    width: usize,
    height: usize,
    line_size: usize,
    mapped_size: usize,
    pixel_format: PixelFormat,
) -> Result<(u32, u32, usize)> {
    if width == 0 || height == 0 {
        return_errno_with_message!(Errno::EINVAL, "framebuffer dimensions are empty");
    }

    // Only 32-bit BGRX is accepted. The other formats the bootloader can report
    // are narrower, so the copy would have to convert rather than move bytes,
    // and a backend that silently reinterpreted them would produce a picture
    // with the wrong colors instead of an error.
    if pixel_format != PixelFormat::BgrReserved {
        return_errno_with_message!(Errno::EOPNOTSUPP, "firmware scanout requires BGRX8888");
    }

    // Every check below is sized in `usize`, which is the width the mapping and
    // the stride arrive in, so the conversion to `u32` happens last and cannot
    // silently wrap the arithmetic.
    let row_bytes = width
        .checked_mul(BGRX8888_BYTES_PER_PIXEL)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "framebuffer row size overflows"))?;
    // A stride below one row would make the rows overlap as they were copied.
    if line_size < row_bytes {
        return_errno_with_message!(Errno::EINVAL, "firmware framebuffer stride is too small");
    }

    // Only the bytes the mode actually addresses have to be mapped; a stride
    // padded past `row_bytes` means the last row ends before `line_size` does.
    let visible_size = (height - 1)
        .checked_mul(line_size)
        .and_then(|offset| offset.checked_add(row_bytes))
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "framebuffer extent overflows"))?;
    if visible_size > mapped_size {
        return_errno_with_message!(Errno::EINVAL, "firmware framebuffer mapping is too small");
    }

    let width = u32::try_from(width)
        .map_err(|_| Error::with_message(Errno::EINVAL, "framebuffer width overflows"))?;
    let height = u32::try_from(height)
        .map_err(|_| Error::with_message(Errno::EINVAL, "framebuffer height overflows"))?;

    Ok((width, height, row_bytes))
}

impl ScanoutBackend for FirmwareFramebufferBackend {
    fn dimensions(&self) -> (u32, u32) {
        (self.width, self.height)
    }

    fn fixed_mode(&self) -> Option<(u32, u32)> {
        Some(self.dimensions())
    }

    fn present_framebuffer(&self, buffer: ScanoutBuffer) -> Result<()> {
        self.validate_buffer(&buffer)?;
        let started = aster_time::read_monotonic_time();
        let (source, height, pitch) = (&buffer.source, buffer.height, buffer.pitch_bytes);
        let mut scratch_row = self.scratch_row.lock();
        for row in 0..height as usize {
            let source_offset = row
                .checked_mul(pitch)
                .and_then(|offset| offset.checked_add(buffer.source_offset_bytes))
                .ok_or_else(|| {
                    Error::with_message(Errno::EINVAL, "scanout source offset overflows")
                })?;
            let mut writer = VmWriter::from(scratch_row.as_mut_slice()).to_fallible();
            source.read(source_offset, &mut writer)?;

            let destination_offset =
                row.checked_mul(self.framebuffer.line_size())
                    .ok_or_else(|| {
                        Error::with_message(Errno::EINVAL, "scanout destination offset overflows")
                    })?;
            self.framebuffer
                .write_bytes_at(destination_offset, scratch_row.as_slice())?;
        }
        let copied_bytes =
            u64::try_from(self.row_bytes.saturating_mul(height as usize)).unwrap_or(u64::MAX);
        let finished = aster_time::read_monotonic_time();
        let report = self.record_present(PresentKind::Full, copied_bytes, started, finished);
        drop(scratch_row);
        if let Some(report) = report {
            Self::log_present(report);
        }
        Ok(())
    }

    /// Copies only the damaged rectangles.
    ///
    /// This is where the trait's incremental hook pays for itself: the copy is
    /// done by the CPU, so the difference between one cursor-sized rectangle
    /// and a 1920x1080 frame is the difference between a smooth pointer and a
    /// visibly lagging one.
    fn dirty_framebuffer(&self, buffer: ScanoutBuffer, damage: &[DamageRect]) -> Result<()> {
        // An empty damage list means "everything changed" — it is what a client
        // passes when it cannot say, so it must not be read as "nothing did".
        if damage.is_empty() {
            return self.present_framebuffer(buffer);
        }
        self.validate_buffer(&buffer)?;
        let copied_bytes = damage
            .iter()
            .fold(0u64, |sum, rect| sum.saturating_add(rect.copied_bytes()));
        let started = aster_time::read_monotonic_time();

        let (source, pitch, base) = (
            &buffer.source,
            buffer.pitch_bytes,
            buffer.source_offset_bytes,
        );
        let mut scratch_row = self.scratch_row.lock();
        for rect in damage {
            let x_offset = (rect.x1 as usize)
                .checked_mul(BGRX8888_BYTES_PER_PIXEL)
                .ok_or_else(|| Error::with_message(Errno::EINVAL, "damage offset overflows"))?;
            let span_bytes = ((rect.x2 - rect.x1) as usize)
                .checked_mul(BGRX8888_BYTES_PER_PIXEL)
                .ok_or_else(|| Error::with_message(Errno::EINVAL, "damage width overflows"))?;

            for row in rect.y1 as usize..rect.y2 as usize {
                let source_offset = row
                    .checked_mul(pitch)
                    .and_then(|offset| offset.checked_add(base))
                    .and_then(|offset| offset.checked_add(x_offset))
                    .ok_or_else(|| {
                        Error::with_message(Errno::EINVAL, "damage source offset overflows")
                    })?;
                let mut writer = VmWriter::from(&mut scratch_row[..span_bytes]).to_fallible();
                source.read(source_offset, &mut writer)?;

                let destination_offset = row
                    .checked_mul(self.framebuffer.line_size())
                    .and_then(|offset| offset.checked_add(x_offset))
                    .ok_or_else(|| {
                        Error::with_message(Errno::EINVAL, "damage destination offset overflows")
                    })?;
                self.framebuffer
                    .write_bytes_at(destination_offset, &scratch_row[..span_bytes])?;
            }
        }
        let finished = aster_time::read_monotonic_time();
        let report = self.record_present(PresentKind::Dirty, copied_bytes, started, finished);
        drop(scratch_row);
        if let Some(report) = report {
            Self::log_present(report);
        }
        Ok(())
    }
}

#[cfg(ktest)]
mod tests {
    use core::sync::atomic::{AtomicU32, Ordering};

    use ostd::prelude::ktest;

    use super::*;
    use crate::vm::page_cache::VmoOptions;

    /// A backend that records how it was called, so the dispatch can be
    /// observed without a display device.
    struct FakeScanout {
        presents: Arc<AtomicU32>,
        dirty_presents: Arc<AtomicU32>,
    }

    impl ScanoutBackend for FakeScanout {
        fn dimensions(&self) -> (u32, u32) {
            (1920, 1080)
        }

        fn present_framebuffer(&self, _buffer: ScanoutBuffer) -> Result<()> {
            self.presents.fetch_add(1, Ordering::Relaxed);
            Ok(())
        }

        fn dirty_framebuffer(&self, _buffer: ScanoutBuffer, _damage: &[DamageRect]) -> Result<()> {
            self.dirty_presents.fetch_add(1, Ordering::Relaxed);
            Ok(())
        }
    }

    impl CursorBackend for FakeScanout {
        fn dimensions(&self) -> (u32, u32) {
            (64, 64)
        }

        fn update_cursor(&self, _buffer: CursorScanoutBuffer) -> Result<u32> {
            Ok(7)
        }

        fn move_cursor(&self, _x: i32, _y: i32) -> Result<()> {
            Ok(())
        }

        fn hide_cursor(&self, _x: i32, _y: i32) -> Result<()> {
            Ok(())
        }

        /// Reports "still mine" only for the cursor it handed out, which is the
        /// one property of this method a caller depends on.
        fn clear_cursor(&self, resource_id: u32, _x: i32, _y: i32) -> Result<bool> {
            Ok(resource_id == 7)
        }
    }

    fn a_buffer() -> Result<ScanoutBuffer> {
        let source = VmoOptions::new(4096).alloc()?;
        Ok(ScanoutBuffer::new(source, 0, 128, 4096, 32, 32))
    }

    #[ktest]
    fn kms_operations_dispatch_through_a_backend() {
        let presents = Arc::new(AtomicU32::new(0));
        let dirty_presents = Arc::new(AtomicU32::new(0));
        let backend = Arc::new(FakeScanout {
            presents: Arc::clone(&presents),
            dirty_presents: Arc::clone(&dirty_presents),
        });

        let scanout: Arc<dyn ScanoutBackend> = backend.clone();
        assert_eq!(scanout.dimensions(), (1920, 1080));

        scanout.present_framebuffer(a_buffer().unwrap()).unwrap();
        assert_eq!(presents.load(Ordering::Relaxed), 1);

        let damage = [DamageRect::new(0, 0, 16, 16, 1920, 1080).unwrap()];
        scanout
            .dirty_framebuffer(a_buffer().unwrap(), &damage)
            .unwrap();
        assert_eq!(dirty_presents.load(Ordering::Relaxed), 1);

        // The cursor half is reached through the same object under its own
        // trait, which is how the driver holds it.
        let cursor: Arc<dyn CursorBackend> = backend;
        assert_eq!(cursor.dimensions(), (64, 64));
        let geometry = CursorGeometry {
            width: 64,
            height: 64,
            hot_x: 0,
            hot_y: 0,
        };
        let position = CursorPosition { x: 1, y: 2 };
        assert_eq!(
            cursor
                .update_cursor(CursorScanoutBuffer::new(
                    VmoOptions::new(4096).alloc().unwrap(),
                    0,
                    4096,
                    geometry,
                    position,
                ))
                .unwrap(),
            7
        );
        assert!(cursor.clear_cursor(7, 1, 2).unwrap());
        // A resource id this backend never handed out must not be treated as
        // its own, or closing one file would hide another file's cursor.
        assert!(!cursor.clear_cursor(8, 1, 2).unwrap());
    }

    #[ktest]
    fn a_backend_without_a_cursor_image_keeps_the_default_full_present() {
        /// Only [`ScanoutBackend`] is implemented, which is the shape a
        /// firmware backend has.
        struct ScanoutOnly;

        impl ScanoutBackend for ScanoutOnly {
            fn dimensions(&self) -> (u32, u32) {
                (640, 400)
            }

            fn present_framebuffer(&self, _buffer: ScanoutBuffer) -> Result<()> {
                Ok(())
            }
        }

        let backend = ScanoutOnly;
        let damage = [DamageRect::new(0, 0, 1, 1, 640, 400).unwrap()];
        // The default re-presents in full rather than doing nothing.
        backend
            .dirty_framebuffer(a_buffer().unwrap(), &damage)
            .unwrap();
    }

    #[ktest]
    fn damage_must_lie_inside_the_scanout() {
        assert!(DamageRect::new(0, 0, 1920, 1080, 1920, 1080).is_ok());
        // Empty in one axis, so it names no pixels.
        assert!(DamageRect::new(5, 0, 5, 10, 1920, 1080).is_err());
        // Inverted.
        assert!(DamageRect::new(10, 0, 5, 10, 1920, 1080).is_err());
        // One pixel past the right/bottom edge.
        assert!(DamageRect::new(0, 0, 1921, 1080, 1920, 1080).is_err());
        assert!(DamageRect::new(0, 0, 1920, 1081, 1920, 1080).is_err());
    }

    #[ktest]
    fn firmware_layout_accepts_a_padded_bgrx8888_mode() {
        // The 1920x1080 mode the Megrez board boots with, whose stride is
        // exactly one row of 32-bit pixels.
        assert_eq!(
            validate_firmware_layout(
                1920,
                1080,
                1920 * 4,
                1920 * 1080 * 4,
                PixelFormat::BgrReserved
            )
            .unwrap(),
            (1920, 1080, 1920 * 4)
        );
        // A stride padded past the visible row is legal, but it costs memory
        // rather than saving it: eleven hundred rows of 7744 bytes need more
        // room than the visible 1920x1080 of pixels does. The mapping that
        // suffices for the unpadded mode is therefore *too small* here, and
        // the extent the mode reaches is (height-1) strides plus one row.
        let padded_stride = 1920 * 4 + 64;
        let padded_extent = (1080 - 1) * padded_stride + 1920 * 4;
        assert!(padded_extent > 1920 * 1080 * 4);
        assert_eq!(
            validate_firmware_layout(
                1920,
                1080,
                padded_stride,
                padded_extent,
                PixelFormat::BgrReserved,
            )
            .unwrap(),
            (1920, 1080, 1920 * 4)
        );
        assert!(validate_firmware_layout(
            1920,
            1080,
            padded_stride,
            1920 * 1080 * 4,
            PixelFormat::BgrReserved,
        )
        .is_err());
    }

    #[ktest]
    fn firmware_layout_accepts_the_megrez_board_mode() {
        // The board's own numbers, from `MEGREZ_FRAMEBUFFER` in
        // tools/riscv/megrez_board_session.py: 1920x1080 at 0xfd800000, a
        // stride of exactly one row, `x8r8g8b8` — which is `BgrReserved`.
        //
        // This is the one board fact that can be checked without a board, and
        // it is worth checking: the board's framebuffer is not the QEMU one,
        // so no amount of passing on QEMU says the driver would have accepted
        // the layout it will actually be handed. A refusal here is a board
        // bring-up that fails at the first present with EINVAL.
        //
        // What it does not cover is equally worth stating: the board's region
        // is at an address QEMU has no device at, so the virtual runs exercise
        // the copy path at a synthesized geometry, never at this one.
        assert_eq!(
            validate_firmware_layout(1920, 1080, 7680, 1920 * 1080 * 4, PixelFormat::BgrReserved)
                .unwrap(),
            (1920, 1080, 7680)
        );
    }

    #[ktest]
    fn firmware_layout_refuses_what_the_copy_could_not_honour() {
        // A stride narrower than one row would make rows overlap.
        assert!(validate_firmware_layout(
            1920,
            1080,
            1920 * 3,
            1920 * 1080 * 4,
            PixelFormat::BgrReserved
        )
        .is_err());
        // Arithmetic that would overflow rather than a large-but-valid mode.
        assert!(
            validate_firmware_layout(1, 2, usize::MAX, usize::MAX, PixelFormat::BgrReserved)
                .is_err()
        );
        // A mapping one byte short of the last pixel the mode addresses.
        assert!(validate_firmware_layout(
            1920,
            1080,
            1920 * 4,
            1920 * 1080 * 4 - 1,
            PixelFormat::BgrReserved,
        )
        .is_err());
        // A format this backend would have to convert, not copy.
        assert!(validate_firmware_layout(
            1920,
            1080,
            1920 * 4,
            1920 * 1080 * 4,
            PixelFormat::Rgb888
        )
        .is_err());
        // A mode with no pixels.
        assert!(
            validate_firmware_layout(0, 1080, 0, 1920 * 1080 * 4, PixelFormat::BgrReserved)
                .is_err()
        );
    }

    #[ktest]
    fn a_padded_stride_is_only_mapped_where_the_mode_reaches() {
        // With a stride padded past the visible row, the last row ends before
        // the stride does, so a mapping that covers exactly the visible pixels
        // must be accepted rather than measured against the full stride.
        let width = 16;
        let height = 8;
        let stride = width * 4 + 16;
        let visible = (height - 1) * stride + width * 4;
        assert!(
            validate_firmware_layout(width, height, stride, visible, PixelFormat::BgrReserved)
                .is_ok()
        );
        assert!(validate_firmware_layout(
            width,
            height,
            stride,
            visible - 1,
            PixelFormat::BgrReserved
        )
        .is_err());
    }

    #[ktest]
    fn firmware_present_stats_separate_full_and_dirty_copies() {
        let mut stats = FirmwarePresentStats::default();
        assert!(stats
            .record_success(PresentKind::Full, 8_294_400, 20_000_000, Duration::ZERO)
            .is_some());
        assert!(stats
            .record_success(PresentKind::Dirty, 1_024, 50_000, Duration::from_secs(1))
            .is_some());
        assert!(stats
            .record_success(PresentKind::Dirty, 2_048, 70_000, Duration::from_secs(2))
            .is_none());
        assert!(stats
            .record_success(
                PresentKind::Full,
                8_294_400,
                18_000_000,
                Duration::from_secs(3)
            )
            .is_some());

        assert_eq!(stats.full.count, 2);
        assert_eq!(stats.full.bytes, 16_588_800);
        assert_eq!(stats.full.total_ns, 38_000_000);
        assert_eq!(stats.full.max_ns, 20_000_000);
        assert_eq!(stats.dirty.count, 2);
        assert_eq!(stats.dirty.bytes, 3_072);
        assert_eq!(stats.dirty.total_ns, 120_000);
        assert_eq!(stats.dirty.max_ns, 70_000);
    }

    #[ktest]
    fn firmware_present_stats_saturate_without_losing_maximum() {
        let mut stats = FirmwarePresentStats::default();
        stats.full.count = u64::MAX - 1;
        stats.full.bytes = u64::MAX - 1;
        stats.full.total_ns = u64::MAX - 1;
        stats.record_success(PresentKind::Full, 4, 7, Duration::ZERO);
        stats.record_success(PresentKind::Full, 4, 3, Duration::from_secs(1));

        assert_eq!(stats.full.count, u64::MAX);
        assert_eq!(stats.full.bytes, u64::MAX);
        assert_eq!(stats.full.total_ns, u64::MAX);
        assert_eq!(stats.full.max_ns, 7);
    }

    #[ktest]
    fn dirty_copy_bytes_count_the_region_actually_copied() {
        let small = DamageRect::new(0, 0, 16, 16, 1920, 1080).unwrap();
        let wide = DamageRect::new(0, 4, 1920, 8, 1920, 1080).unwrap();
        assert_eq!(small.copied_bytes(), 1_024);
        assert_eq!(wide.copied_bytes(), 30_720);
        // Overlapping clips cause overlapping copies, so both count.
        assert_eq!(small.copied_bytes() + wide.copied_bytes(), 31_744);
    }

    #[ktest]
    fn firmware_present_stats_report_after_five_seconds_without_power_of_two() {
        let mut stats = FirmwarePresentStats::default();
        for second in 0..4 {
            stats.record_success(PresentKind::Full, 4, 1, Duration::from_secs(second));
        }
        assert!(stats
            .record_success(PresentKind::Dirty, 4, 1, Duration::from_secs(7))
            .is_none());
        let report = stats
            .record_success(PresentKind::Dirty, 4, 1, Duration::from_secs(8))
            .expect("a short observation window needs a bounded report");
        assert_eq!(report.total_successes, 6);
        assert!(stats
            .record_success(PresentKind::Dirty, 4, 1, Duration::from_secs(9))
            .is_none());
    }
}
