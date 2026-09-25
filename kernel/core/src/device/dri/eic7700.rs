// SPDX-License-Identifier: MPL-2.0

//! Read-only EIC7700 display handoff inventory for a selected development boot.
//!
//! The firmware framebuffer works without owning the display controller. Before
//! a native scanout backend can replace its copy loop, a selected boot needs to
//! establish which physical address, stride, and mode the controller actually
//! scans, and whether the DRM dumb-buffer pool is DMA-reachable. This module
//! reads those registers once when `asterinas.dc_probe=1` is on the command
//! line. It never writes display registers or changes the active backend.

use ostd::{
    arch::boot::DEVICE_TREE,
    boot::boot_info,
    io::IoMem,
    mm::{VmIoOnce, dma::sync_eic7700_dram_to_device},
};

use crate::vm::page_cache::Vmo;

const DC_REG_START: usize = 0x502c_1400;
const DC_REG_SIZE: usize = 0x1400;
const FULL_HD_FRAME_BYTES: usize = 1920 * 1080 * 4;
const DMA_CLEAN_CHUNK_BYTES: usize = 256 * 1024;

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
