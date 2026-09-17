// SPDX-License-Identifier: MPL-2.0

//! Device-specific presentation behind the generic DRM/KMS state machine.
//!
//! [`ScanoutBackend`] presents framebuffers prepared and validated by `kms`,
//! while the optional [`CursorBackend`] exposes hardware-cursor operations.
//! Virtio-gpu implements both contracts; [`FirmwareFramebufferBackend`]
//! implements scanout by copying into the mode and memory left active by firmware.

use aster_framebuffer::{framebuffer::FrameBuffer, pixel::PixelFormat};
use aster_virtio::device::gpu::device::GpuDevice;
use ostd::mm::HasSize;

use super::cursor;
use crate::{prelude::*, vm::page_cache::Vmo};

const BGRX8888_BYTES_PER_PIXEL: usize = 4;

pub(super) trait DrmBackingOwner: Send + Sync {}

impl<T: Send + Sync> DrmBackingOwner for T {}

struct VirtioBackingOwner {
    _owner: Arc<dyn DrmBackingOwner>,
}

/// A backend-neutral framebuffer whose lifetime is pinned during presentation.
pub(super) struct ScanoutBuffer {
    source: Arc<Vmo>,
    source_offset_bytes: usize,
    pitch_bytes: usize,
    size_bytes: u32,
    owner: Arc<dyn DrmBackingOwner>,
    width: u32,
    height: u32,
}

impl ScanoutBuffer {
    pub(super) fn new(
        source: Arc<Vmo>,
        source_offset_bytes: usize,
        pitch_bytes: usize,
        size_bytes: u32,
        owner: Arc<dyn DrmBackingOwner>,
        width: u32,
        height: u32,
    ) -> Self {
        Self {
            source,
            source_offset_bytes,
            pitch_bytes,
            size_bytes,
            owner,
            width,
            height,
        }
    }

    pub(super) fn dimensions(&self) -> (u32, u32) {
        (self.width, self.height)
    }
}

/// A backend-neutral cursor buffer whose lifetime is pinned during presentation.
pub(super) struct CursorScanoutBuffer {
    source: Arc<Vmo>,
    source_offset_bytes: usize,
    size_bytes: u32,
    owner: Arc<dyn DrmBackingOwner>,
    width: u32,
    height: u32,
    hot_x: u32,
    hot_y: u32,
    x: i32,
    y: i32,
}

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
        owner: Arc<dyn DrmBackingOwner>,
        geometry: CursorGeometry,
        position: cursor::CursorPosition,
    ) -> Self {
        Self {
            source,
            source_offset_bytes,
            size_bytes,
            owner,
            width: geometry.width,
            height: geometry.height,
            hot_x: geometry.hot_x,
            hot_y: geometry.hot_y,
            x: position.x,
            y: position.y,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct DamageRect {
    x1: u32,
    y1: u32,
    x2: u32,
    y2: u32,
}

impl DamageRect {
    pub(super) fn new(x1: u32, y1: u32, x2: u32, y2: u32, width: u32, height: u32) -> Result<Self> {
        if x1 >= x2 || y1 >= y2 || x2 > width || y2 > height {
            return_errno_with_message!(Errno::EINVAL, "damage is outside the scanout");
        }
        Ok(Self { x1, y1, x2, y2 })
    }

    pub(super) fn area(self) -> u64 {
        u64::from(self.x2 - self.x1) * u64::from(self.y2 - self.y1)
    }
}

/// Hardware operations required by the generic single-pipeline KMS state.
pub(super) trait ScanoutBackend: Send + Sync {
    /// Returns the active scanout dimensions in pixels.
    fn dimensions(&self) -> (u32, u32);

    /// Presents one linear framebuffer on the active scanout.
    fn present_framebuffer(&self, buffer: ScanoutBuffer) -> Result<()>;

    /// Re-presents the active framebuffer after damage.
    ///
    /// Backends that do not override this method redraw the complete framebuffer.
    fn dirty_framebuffer(&self, buffer: ScanoutBuffer, _damage: &[DamageRect]) -> Result<()> {
        self.present_framebuffer(buffer)
    }

    /// Disables the active scanout.
    fn disable_scanout(&self) -> Result<()>;
}

/// Optional hardware-cursor operations implemented by capable display backends.
pub(super) trait CursorBackend: Send + Sync {
    /// Returns the maximum hardware-cursor dimensions.
    fn dimensions(&self) -> (u32, u32);

    /// Replaces the hardware cursor image and position.
    fn update_cursor(&self, buffer: CursorScanoutBuffer) -> Result<u32>;

    /// Moves the active hardware cursor.
    fn move_cursor(&self, x: i32, y: i32) -> Result<()>;

    /// Hides the active hardware cursor.
    fn hide_cursor(&self, x: i32, y: i32) -> Result<()>;

    /// Hides the cursor only when `resource_id` is still active.
    fn clear_cursor(&self, resource_id: u32, x: i32, y: i32) -> Result<bool>;
}

impl ScanoutBackend for GpuDevice {
    fn dimensions(&self) -> (u32, u32) {
        (GpuDevice::width(self), GpuDevice::height(self))
    }

    fn present_framebuffer(&self, buffer: ScanoutBuffer) -> Result<()> {
        let ScanoutBuffer {
            source,
            source_offset_bytes,
            size_bytes,
            owner,
            width,
            height,
            ..
        } = buffer;
        let addr = source
            .paddr()
            .and_then(|base| base.checked_add(source_offset_bytes))
            .ok_or_else(|| {
                Error::with_message(
                    Errno::ENOMEM,
                    "virtio-gpu scanout backing is not contiguous",
                )
            })? as u64;
        let owner = Arc::new(VirtioBackingOwner { _owner: owner });
        GpuDevice::present_framebuffer(self, addr, size_bytes, owner, width, height)
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu present failed"))
    }

    fn disable_scanout(&self) -> Result<()> {
        GpuDevice::disable_scanout(self)
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu disable failed"))
    }
}

impl CursorBackend for GpuDevice {
    fn dimensions(&self) -> (u32, u32) {
        (cursor::CURSOR_SIZE, cursor::CURSOR_SIZE)
    }

    fn update_cursor(&self, buffer: CursorScanoutBuffer) -> Result<u32> {
        let CursorScanoutBuffer {
            source,
            source_offset_bytes,
            size_bytes,
            owner,
            width,
            height,
            hot_x,
            hot_y,
            x,
            y,
        } = buffer;
        let addr = source
            .paddr()
            .and_then(|base| base.checked_add(source_offset_bytes))
            .ok_or_else(|| {
                Error::with_message(Errno::ENOMEM, "virtio-gpu cursor backing is not contiguous")
            })? as u64;
        let owner = Arc::new(VirtioBackingOwner { _owner: owner });
        GpuDevice::update_cursor(
            self, addr, size_bytes, owner, width, height, hot_x, hot_y, x, y,
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

/// A backend that copies DRM dumb buffers into the fixed framebuffer left active by firmware.
///
/// This backend deliberately does not program EIC7700 display registers.
/// It provides the first physical-board KMS path while firmware continues to own the mode and HDMI link.
/// A later native backend can replace it without changing the generic DRM/KMS state machine.
pub(super) struct FirmwareFramebufferBackend {
    framebuffer: Arc<FrameBuffer>,
    width: u32,
    height: u32,
    row_bytes: usize,
    scratch_row: Mutex<Vec<u8>>,
}

impl FirmwareFramebufferBackend {
    pub(super) fn new(framebuffer: Arc<FrameBuffer>) -> Result<Self> {
        let (width, height, row_bytes) = validate_firmware_layout(
            framebuffer.width(),
            framebuffer.height(),
            framebuffer.line_size(),
            framebuffer.io_mem().size(),
            framebuffer.pixel_format(),
        )?;

        Ok(Self {
            framebuffer,
            width,
            height,
            row_bytes,
            scratch_row: Mutex::new(vec![0; row_bytes]),
        })
    }
}

fn validate_firmware_layout(
    width: usize,
    height: usize,
    line_size: usize,
    mapped_size: usize,
    pixel_format: PixelFormat,
) -> Result<(u32, u32, usize)> {
    let width = u32::try_from(width)
        .map_err(|_| Error::with_message(Errno::EINVAL, "framebuffer width overflows"))?;
    let height = u32::try_from(height)
        .map_err(|_| Error::with_message(Errno::EINVAL, "framebuffer height overflows"))?;
    if width == 0 || height == 0 {
        return_errno_with_message!(Errno::EINVAL, "framebuffer dimensions are empty");
    }
    let row_bytes = (width as usize)
        .checked_mul(BGRX8888_BYTES_PER_PIXEL)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "framebuffer row size overflows"))?;
    if pixel_format != PixelFormat::BgrReserved {
        return_errno_with_message!(Errno::EOPNOTSUPP, "DRM firmware scanout requires BGRX8888");
    }
    if line_size < row_bytes {
        return_errno_with_message!(Errno::EINVAL, "firmware framebuffer stride is too small");
    }
    let visible_size = (height as usize - 1)
        .checked_mul(line_size)
        .and_then(|offset| offset.checked_add(row_bytes))
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "framebuffer extent overflows"))?;
    if visible_size > mapped_size {
        return_errno_with_message!(Errno::EINVAL, "firmware framebuffer mapping is too small");
    }
    Ok((width, height, row_bytes))
}

impl ScanoutBackend for FirmwareFramebufferBackend {
    fn dimensions(&self) -> (u32, u32) {
        (self.width, self.height)
    }

    fn present_framebuffer(&self, buffer: ScanoutBuffer) -> Result<()> {
        self.validate_buffer(&buffer)?;
        let ScanoutBuffer {
            source,
            source_offset_bytes,
            pitch_bytes,
            owner: _owner,
            height,
            ..
        } = buffer;
        let mut scratch_row = self.scratch_row.lock();
        for row in 0..height as usize {
            let row_offset = source_offset_bytes
                .checked_add(row.checked_mul(pitch_bytes).ok_or_else(|| {
                    Error::with_message(Errno::EINVAL, "scanout row offset overflows")
                })?)
                .ok_or_else(|| {
                    Error::with_message(Errno::EINVAL, "scanout source offset overflows")
                })?;
            let mut writer = VmWriter::from(scratch_row.as_mut_slice()).to_fallible();
            source.read(row_offset, &mut writer)?;
            let destination_offset =
                row.checked_mul(self.framebuffer.line_size())
                    .ok_or_else(|| {
                        Error::with_message(Errno::EINVAL, "scanout destination offset overflows")
                    })?;
            self.framebuffer
                .write_bytes_at(destination_offset, scratch_row.as_slice())?;
        }
        Ok(())
    }

    fn dirty_framebuffer(&self, buffer: ScanoutBuffer, damage: &[DamageRect]) -> Result<()> {
        if damage.is_empty() {
            return self.present_framebuffer(buffer);
        }
        self.validate_buffer(&buffer)?;
        let ScanoutBuffer {
            source,
            source_offset_bytes,
            pitch_bytes,
            owner: _owner,
            ..
        } = buffer;
        let mut scratch_row = self.scratch_row.lock();
        for rect in damage {
            let x_offset = (rect.x1 as usize)
                .checked_mul(BGRX8888_BYTES_PER_PIXEL)
                .ok_or_else(|| Error::with_message(Errno::EINVAL, "damage offset overflows"))?;
            let span_bytes = ((rect.x2 - rect.x1) as usize)
                .checked_mul(BGRX8888_BYTES_PER_PIXEL)
                .ok_or_else(|| Error::with_message(Errno::EINVAL, "damage width overflows"))?;
            for row in rect.y1 as usize..rect.y2 as usize {
                let source_offset = source_offset_bytes
                    .checked_add(row.checked_mul(pitch_bytes).ok_or_else(|| {
                        Error::with_message(Errno::EINVAL, "damage row offset overflows")
                    })?)
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
        Ok(())
    }

    fn disable_scanout(&self) -> Result<()> {
        // Firmware owns the fixed mode and HDMI link, so logical disable only
        // stops DRM presentation.  Reprogramming or powering down the link is
        // reserved for the future native EIC7700 backend.
        Ok(())
    }
}

impl FirmwareFramebufferBackend {
    fn validate_buffer(&self, buffer: &ScanoutBuffer) -> Result<()> {
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

#[cfg(ktest)]
mod tests {
    use core::sync::atomic::{AtomicU32, Ordering};

    use ostd::prelude::ktest;

    use super::*;
    use crate::vm::page_cache::VmoOptions;

    struct FakeScanout {
        presents: Arc<AtomicU32>,
    }

    impl ScanoutBackend for FakeScanout {
        fn dimensions(&self) -> (u32, u32) {
            (1920, 1080)
        }

        fn present_framebuffer(&self, _buffer: ScanoutBuffer) -> Result<()> {
            self.presents.fetch_add(1, Ordering::Relaxed);
            Ok(())
        }

        fn disable_scanout(&self) -> Result<()> {
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

        fn clear_cursor(&self, resource_id: u32, _x: i32, _y: i32) -> Result<bool> {
            Ok(resource_id == 7)
        }
    }

    #[ktest]
    fn generic_kms_operations_dispatch_through_a_backend() {
        let presents = Arc::new(AtomicU32::new(0));
        let backend = Arc::new(FakeScanout {
            presents: Arc::clone(&presents),
        });
        let scanout_backend: Arc<dyn ScanoutBackend> = backend.clone();

        assert_eq!(scanout_backend.dimensions(), (1920, 1080));
        let source = VmoOptions::new(4096).alloc().unwrap();
        scanout_backend
            .present_framebuffer(ScanoutBuffer::new(
                source,
                0,
                128,
                4096,
                Arc::new(()),
                32,
                32,
            ))
            .unwrap();
        assert_eq!(presents.load(Ordering::Relaxed), 1);
        let cursor_backend: Arc<dyn CursorBackend> = backend;
        let source = VmoOptions::new(4096).alloc().unwrap();
        assert_eq!(
            cursor_backend
                .update_cursor(CursorScanoutBuffer::new(
                    source,
                    0,
                    4096,
                    Arc::new(()),
                    CursorGeometry {
                        width: 64,
                        height: 64,
                        hot_x: 0,
                        hot_y: 0,
                    },
                    cursor::CursorPosition { x: 1, y: 2 },
                ))
                .unwrap(),
            7
        );
        assert!(cursor_backend.clear_cursor(7, 1, 2).unwrap());
    }

    #[ktest]
    fn firmware_layout_accepts_megrez_bgrx8888_mode() {
        assert_eq!(
            validate_firmware_layout(
                1920,
                1080,
                1920 * 4,
                1920 * 1080 * 4,
                PixelFormat::BgrReserved,
            )
            .unwrap(),
            (1920, 1080, 1920 * 4)
        );
        assert!(
            validate_firmware_layout(
                1920,
                1080,
                1920 * 3,
                1920 * 1080 * 4,
                PixelFormat::BgrReserved,
            )
            .is_err()
        );
        assert!(
            validate_firmware_layout(1, 2, usize::MAX, usize::MAX, PixelFormat::BgrReserved,)
                .is_err()
        );
        assert!(
            validate_firmware_layout(
                1920,
                1080,
                1920 * 4,
                1920 * 1080 * 4 - 1,
                PixelFormat::BgrReserved,
            )
            .is_err()
        );
        assert!(
            validate_firmware_layout(1920, 1080, 1920 * 4, 1920 * 1080 * 4, PixelFormat::Rgb888,)
                .is_err()
        );
    }
}
