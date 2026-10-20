// SPDX-License-Identifier: MPL-2.0

//! EIC7700 firmware-mode handoff probe and opt-in native primary scanout.
//!
//! The firmware framebuffer works without owning the display controller. Before
//! a native scanout backend can replace its copy loop, a selected boot needs to
//! establish which physical address, stride, and mode the controller actually
//! scans, and whether the DRM dumb-buffer pool is DMA-reachable. This module
//! reads those registers once when `asterinas.dc_probe=1` is on the command
//! line. The separate `asterinas.dc_native_scanout=1` experiment takes ownership
//! of only the primary address register after validating the live mode.

use core::ops::Range;

use aster_framebuffer::framebuffer::FrameBuffer;
use ostd::{
    arch::boot::DEVICE_TREE,
    boot::boot_info,
    io::IoMem,
    mm::{HasPaddr, VmIoOnce, dma::sync_eic7700_dram_to_device},
};

use super::{
    backend::{DamageRect, FirmwareFramebufferBackend, ScanoutBackend, ScanoutBuffer},
    eic7700_contract::checked_scanout_span,
};
use crate::{prelude::*, vm::page_cache::Vmo};

const DC_REG_START: usize = 0x502c_1400;
const DC_REG_SIZE: usize = 0x1400;
const FULL_HD_FRAME_BYTES: usize = 1920 * 1080 * 4;
const DMA_CLEAN_CHUNK_BYTES: usize = 256 * 1024;
const PRIMARY_ADDRESS: usize = 0x000;
const PRIMARY_STRIDE: usize = 0x008;
const PRIMARY_CONFIG: usize = 0x118;
const DISPLAY_H: usize = 0x030;
const DISPLAY_V: usize = 0x040;
const DC_PITCH_ALIGNMENT: usize = 128;
// The firmware handoff observed on the 1920x1080 Megrez desktop. This masks
// status-only underflow/flip bits below, while failing closed on tile, scale,
// rotation, swizzle, clear, and format changes we have not validated.
const MEGREZ_LINEAR_CONFIG: u32 = 0x1800_4011;
const DC_STATUS_BITS: u32 = (1 << 5) | (1 << 6);

/// The first native display stage adopts firmware's mode and only changes the
/// primary plane address. It must be explicitly selected for a recovery boot.
pub(super) fn native_scanout_requested() -> bool {
    boot_info()
        .kernel_cmdline
        .split_whitespace()
        .any(|word| word == "asterinas.dc_native_scanout=1")
}

struct NativeState {
    displayed: Option<ScanoutBuffer>,
    presents: u64,
    dirties: u64,
    clean_ns: u64,
}

/// Experimental fixed-mode DC scanout. Firmware continues to own HDMI and
/// timing, and its framebuffer remains mapped for a reversible handoff.
pub(super) struct Eic7700Scanout {
    registers: IoMem,
    firmware_address: u32,
    mode_width: u32,
    mode_height: u32,
    mode_pitch: usize,
    state: Mutex<NativeState>,
}

impl Eic7700Scanout {
    pub(super) fn new(framebuffer: &FrameBuffer) -> Result<Self> {
        if !native_scanout_requested() {
            return_errno_with_message!(Errno::EOPNOTSUPP, "native scanout was not requested");
        }
        if !FirmwareFramebufferBackend::accepts(framebuffer) {
            return_errno_with_message!(Errno::EOPNOTSUPP, "firmware pixel layout is unsupported");
        }
        let tree = DEVICE_TREE
            .get()
            .ok_or_else(|| Error::with_message(Errno::ENODEV, "no device tree"))?;
        let node = tree
            .find_compatible(&["eswin,dc"])
            .ok_or_else(|| Error::with_message(Errno::ENODEV, "no EIC7700 display controller"))?;
        let region = node
            .reg()
            .and_then(|mut regions| regions.nth(2))
            .ok_or_else(|| Error::with_message(Errno::ENODEV, "no DC register aperture"))?;
        if region.starting_address as usize != DC_REG_START || region.size != Some(DC_REG_SIZE) {
            return_errno_with_message!(Errno::ENODEV, "unexpected DC register aperture");
        }
        let registers = IoMem::acquire(DC_REG_START..DC_REG_START + DC_REG_SIZE)
            .map_err(|_| Error::with_message(Errno::EBUSY, "DC registers unavailable"))?;
        let read = |offset| {
            registers
                .read_once::<u32>(offset)
                .map_err(|_| Error::with_message(Errno::EIO, "DC register read failed"))
        };
        let firmware_address = read(PRIMARY_ADDRESS)?;
        let stride = read(PRIMARY_STRIDE)? as usize;
        let config = read(PRIMARY_CONFIG)?;
        let display_h = read(DISPLAY_H)?;
        let display_v = read(DISPLAY_V)?;
        let width = u32::try_from(framebuffer.width())
            .map_err(|_| Error::with_message(Errno::EINVAL, "firmware width overflows"))?;
        let height = u32::try_from(framebuffer.height())
            .map_err(|_| Error::with_message(Errno::EINVAL, "firmware height overflows"))?;
        if firmware_address as usize != framebuffer.io_mem().paddr()
            || stride != framebuffer.line_size()
            || width != display_h & 0xffff
            || height != display_v & 0xffff
            || width.checked_mul(4).map(|v| v as usize) != Some(stride)
            || stride % DC_PITCH_ALIGNMENT != 0
            || config & !DC_STATUS_BITS != MEGREZ_LINEAR_CONFIG
            || config & (1 << 6) != 0
        {
            return_errno_with_message!(Errno::EINVAL, "firmware DC mode is not safe to adopt");
        }
        ostd::info!(
            "ASTERINAS_DC_NATIVE ready firmware_addr={:#x} mode={}x{} pitch={} config={:#x}",
            firmware_address,
            width,
            height,
            stride,
            config,
        );
        Ok(Self {
            registers,
            firmware_address,
            mode_width: width,
            mode_height: height,
            mode_pitch: stride,
            state: Mutex::new(NativeState {
                displayed: None,
                presents: 0,
                dirties: 0,
                clean_ns: 0,
            }),
        })
    }

    fn clean_range(range: Range<usize>) -> Result<()> {
        for start in (range.start..range.end).step_by(DMA_CLEAN_CHUNK_BYTES) {
            sync_eic7700_dram_to_device(
                start..range.end.min(start.saturating_add(DMA_CLEAN_CHUNK_BYTES)),
            )
            .map_err(|_| Error::with_message(Errno::EIO, "DC DMA cache clean failed"))?;
        }
        Ok(())
    }

    fn clean_damage(&self, frame: Range<usize>, damage: &[DamageRect]) -> Result<()> {
        if damage.is_empty() {
            return Self::clean_range(frame);
        }
        for rect in damage {
            let (x1, y1, x2, y2) = rect.bounds();
            let x_start = (x1 as usize)
                .checked_mul(4)
                .ok_or_else(|| Error::with_message(Errno::EINVAL, "damage X offset overflows"))?;
            let x_end = (x2 as usize)
                .checked_mul(4)
                .ok_or_else(|| Error::with_message(Errno::EINVAL, "damage X end overflows"))?;
            if x1 == 0 && x2 == self.mode_width {
                let start = frame.start + y1 as usize * self.mode_pitch;
                let end = frame.start + y2 as usize * self.mode_pitch;
                Self::clean_range(start..end)?;
            } else {
                for y in y1..y2 {
                    let row = frame.start + y as usize * self.mode_pitch;
                    Self::clean_range(row + x_start..row + x_end)?;
                }
            }
        }
        Ok(())
    }

    fn submit(&self, buffer: ScanoutBuffer, damage: Option<&[DamageRect]>) -> Result<()> {
        let address = usize::try_from(buffer.paddr()?)
            .map_err(|_| Error::with_message(Errno::EINVAL, "scanout address overflows"))?;
        let range = checked_scanout_span(
            address,
            buffer.size_bytes(),
            buffer.dimensions().0,
            buffer.dimensions().1,
            buffer.pitch_bytes(),
            self.mode_width,
            self.mode_height,
        )
        .ok_or_else(|| {
            ostd::warn!(
                "ASTERINAS_DC_NATIVE reject addr={:#x} size={} width={} height={} pitch={} mode={}x{}",
                address,
                buffer.size_bytes(),
                buffer.dimensions().0,
                buffer.dimensions().1,
                buffer.pitch_bytes(),
                self.mode_width,
                self.mode_height,
            );
            Error::with_message(Errno::EINVAL, "invalid native scanout buffer")
        })?;
        if buffer.pitch_bytes() != self.mode_pitch {
            return_errno_with_message!(Errno::EINVAL, "scanout pitch differs from DC mode");
        }

        // Serialize cache synchronization with address updates. The sleeping
        // mutex also keeps the displayed VMO alive until the next submission.
        let mut state = self.state.lock();
        let started = aster_time::read_monotonic_time();
        let was_displayed = state
            .displayed
            .as_ref()
            .is_some_and(|previous| previous.paddr().ok() == Some(address as u64));
        if was_displayed {
            match damage {
                Some(rects) => self.clean_damage(range.clone(), rects)?,
                None => Self::clean_range(range.clone())?,
            }
        } else {
            Self::clean_range(range.clone())?;
        }
        let cleaned_at = aster_time::read_monotonic_time();
        let old_address = self
            .registers
            .read_once::<u32>(PRIMARY_ADDRESS)
            .map_err(|_| Error::with_message(Errno::EIO, "DC address read failed"))?;
        if old_address != address as u32 {
            let config = self
                .registers
                .read_once::<u32>(PRIMARY_CONFIG)
                .map_err(|_| Error::with_message(Errno::EIO, "DC config read failed"))?;
            if config & ((1 << 3) | (1 << 6)) != 0 {
                return_errno_with_message!(Errno::EBUSY, "DC shadow or flip is active");
            }
            self.registers
                .write_once(PRIMARY_ADDRESS, &(address as u32))
                .map_err(|_| Error::with_message(Errno::EIO, "DC address write failed"))?;
            if self.registers.read_once::<u32>(PRIMARY_ADDRESS).ok() != Some(address as u32) {
                let _ = self.registers.write_once(PRIMARY_ADDRESS, &old_address);
                return_errno_with_message!(Errno::EIO, "DC address readback failed");
            }
        }
        state.displayed = Some(buffer);
        state.presents = state.presents.saturating_add(u64::from(damage.is_none()));
        state.dirties = state.dirties.saturating_add(u64::from(damage.is_some()));
        state.clean_ns = state.clean_ns.saturating_add(
            u64::try_from(cleaned_at.saturating_sub(started).as_nanos()).unwrap_or(u64::MAX),
        );
        let count = state.presents.saturating_add(state.dirties);
        if count.is_power_of_two() {
            ostd::info!(
                "ASTERINAS_DC_NATIVE present_count={} dirty_count={} clean_total_ns={} addr={:#x} underflow={}",
                state.presents,
                state.dirties,
                state.clean_ns,
                address,
                self.registers.read_once::<u32>(PRIMARY_CONFIG).unwrap_or(0) & (1 << 5) != 0,
            );
        }
        Ok(())
    }
}

impl ScanoutBackend for Eic7700Scanout {
    fn dimensions(&self) -> (u32, u32) {
        (self.mode_width, self.mode_height)
    }

    fn fixed_mode(&self) -> Option<(u32, u32)> {
        Some(self.dimensions())
    }

    fn present_framebuffer(&self, buffer: ScanoutBuffer) -> Result<()> {
        self.submit(buffer, None)
    }

    fn dirty_framebuffer(&self, buffer: ScanoutBuffer, damage: &[DamageRect]) -> Result<()> {
        self.submit(buffer, Some(damage))
    }
}

impl Drop for Eic7700Scanout {
    fn drop(&mut self) {
        if self
            .registers
            .write_once(PRIMARY_ADDRESS, &self.firmware_address)
            .is_err()
        {
            ostd::warn!("ASTERINAS_DC_NATIVE firmware address restore failed");
        }
    }
}

pub(super) fn log_handoff_probe(pool: &Vmo) {
    if !boot_info()
        .kernel_cmdline
        .split_whitespace()
        .any(|word| word == "asterinas.dc_probe=1")
    {
        return;
    }

    let Some(tree) = DEVICE_TREE.get() else {
        ostd::warn!("ASTERINAS_DC_PROBE skipped=no_device_tree");
        return;
    };
    let Some(node) = tree.find_compatible(&["eswin,dc"]) else {
        ostd::warn!("ASTERINAS_DC_PROBE skipped=no_eswin_dc");
        return;
    };
    let Some(region) = node.reg().and_then(|mut regions| regions.nth(2)) else {
        ostd::warn!("ASTERINAS_DC_PROBE skipped=no_register_aperture");
        return;
    };
    if region.starting_address as usize != DC_REG_START || region.size != Some(DC_REG_SIZE) {
        ostd::warn!("ASTERINAS_DC_PROBE skipped=unexpected_register_aperture");
        return;
    }

    let Ok(registers) = IoMem::acquire(DC_REG_START..DC_REG_START + DC_REG_SIZE) else {
        ostd::warn!("ASTERINAS_DC_PROBE skipped=registers_unavailable");
        return;
    };
    let (Ok(primary_addr), Ok(stride), Ok(config), Ok(display_h), Ok(display_v)) = (
        registers.read_once::<u32>(0x000),
        registers.read_once::<u32>(0x008),
        registers.read_once::<u32>(0x118),
        registers.read_once::<u32>(0x030),
        registers.read_once::<u32>(0x040),
    ) else {
        ostd::warn!("ASTERINAS_DC_PROBE skipped=register_read_failed");
        return;
    };

    let Some(pool_addr) = pool.paddr() else {
        ostd::warn!("ASTERINAS_DC_PROBE skipped=pool_not_physical");
        return;
    };
    ostd::info!(
        "ASTERINAS_DC_PROBE primary_addr={:#x} stride={} config={:#x} display_h={:#x} display_v={:#x} pool_addr={:#x} pool_size={}",
        primary_addr,
        stride,
        config,
        display_h,
        display_v,
        pool_addr,
        pool.size(),
    );

    if !boot_info()
        .kernel_cmdline
        .split_whitespace()
        .any(|word| word == "asterinas.dc_dma_probe=1")
    {
        return;
    }

    let Some(end) = pool_addr.checked_add(FULL_HD_FRAME_BYTES) else {
        ostd::warn!("ASTERINAS_DC_DMA_PROBE skipped=address_overflow");
        return;
    };
    if FULL_HD_FRAME_BYTES > pool.size() {
        ostd::warn!("ASTERINAS_DC_DMA_PROBE skipped=pool_too_small");
        return;
    }

    let started = aster_time::read_monotonic_time();
    let clean_result = (pool_addr..end)
        .step_by(DMA_CLEAN_CHUNK_BYTES)
        .try_for_each(|start| {
            sync_eic7700_dram_to_device(start..end.min(start.saturating_add(DMA_CLEAN_CHUNK_BYTES)))
        });
    match clean_result {
        Ok(()) => {
            let elapsed_ns = aster_time::read_monotonic_time()
                .saturating_sub(started)
                .as_nanos();
            ostd::info!(
                "ASTERINAS_DC_DMA_PROBE clean_bytes={} clean_ns={} pool_addr={:#x}",
                FULL_HD_FRAME_BYTES,
                elapsed_ns,
                pool_addr,
            );
        }
        Err(error) => ostd::warn!("ASTERINAS_DC_DMA_PROBE skipped=sync_failed error={error:?}"),
    }
}
