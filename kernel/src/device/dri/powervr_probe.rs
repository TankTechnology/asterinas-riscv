// SPDX-License-Identifier: MPL-2.0

//! Opt-in, device-tree-only PowerVR resource discovery for Megrez.

use core::array;

use ostd::{arch::boot::DEVICE_TREE, boot::boot_info, io::IoMem, mm::VmIoOnce};

mod power;

// Pinned to the prepared Megrez DTB. The GPU must not be touched until its
// clock, reset, and power-domain ownership has been established separately.
const GPU_REG_START: usize = 0x5140_0000;
const GPU_REG_SIZE: usize = 0x0f_ffff;
const GPU_CLOCK_CELLS_BYTES: usize = 3 * 2 * 4;
const GPU_RESET_CELLS_BYTES: usize = 5 * 3 * 4;
const GPU_INTERRUPT_CELLS_BYTES: usize = 4;
// RockOS bf2ec5d5: eswin_cpu/sysconfig.c requests these GPU clocks/resets;
// clk_eic7700.h and reset-eswin.c define the CRG word layout. This stage
// samples the system CRG only. It never reads or writes the GPU aperture.
const CRG_BASE: usize = 0x5182_8000;
const CRG_SIZE: usize = 0x8_0000;
const GPU_ACLK_OFFSET: usize = 0x12c;
const GPU_CFG_OFFSET: usize = 0x130;
const GPU_GRAY_OFFSET: usize = 0x134;
const GPU_RESET_OFFSET: usize = 0x404;
const CRG_GATE_BIT: u32 = 1 << 31;
const GPU_CLOCK_BINDINGS: [u32; 6] = [3, 523, 3, 524, 3, 525];
const GPU_RESET_BINDINGS: [u32; 15] = [20, 1, 1, 20, 1, 2, 20, 1, 4, 20, 1, 8, 20, 1, 16];

#[derive(Clone, Copy)]
struct GpuResources<'a> {
    base: usize,
    size: Option<usize>,
    clocks_bytes: Option<usize>,
    resets_bytes: Option<usize>,
    interrupts_bytes: Option<usize>,
    dma_noncoherent: bool,
    status: Option<&'a str>,
}

fn validate_gpu_resources(resources: GpuResources<'_>) -> Result<(), &'static str> {
    if resources.status != Some("okay") {
        return Err("gpu_node_disabled");
    }
    if resources.base != GPU_REG_START || resources.size != Some(GPU_REG_SIZE) {
        return Err("unexpected_register_aperture");
    }
    if resources.clocks_bytes != Some(GPU_CLOCK_CELLS_BYTES) {
        return Err("unexpected_clock_contract");
    }
    if resources.resets_bytes != Some(GPU_RESET_CELLS_BYTES) {
        return Err("unexpected_reset_contract");
    }
    if resources.interrupts_bytes != Some(GPU_INTERRUPT_CELLS_BYTES) {
        return Err("unexpected_interrupt_contract");
    }
    if !resources.dma_noncoherent {
        return Err("unexpected_dma_contract");
    }
    Ok(())
}

fn inspect_gpu_dt() -> Result<(), &'static str> {
    let tree = DEVICE_TREE.get().ok_or("no_device_tree")?;
    let node = tree.find_compatible(&["img,gpu"]).ok_or("no_gpu_node")?;
    let mut regions = node.reg().ok_or("no_register_aperture")?;
    let region = regions.next().ok_or("no_register_aperture")?;
    if regions.next().is_some() {
        return Err("unexpected_register_aperture");
    }
    validate_gpu_resources(GpuResources {
        base: region.starting_address as usize,
        size: region.size,
        clocks_bytes: node.property("clocks").map(|property| property.value.len()),
        resets_bytes: node.property("resets").map(|property| property.value.len()),
        interrupts_bytes: node
            .property("interrupts")
            .map(|property| property.value.len()),
        dma_noncoherent: node.property("dma-noncoherent").is_some(),
        status: node
            .property("status")
            .and_then(|property| property.as_str()),
    })
}

fn decode_cells<const N: usize>(bytes: &[u8]) -> Option<[u32; N]> {
    let (chunks, remainder) = bytes.as_chunks::<4>();
    if chunks.len() != N || !remainder.is_empty() {
        return None;
    }
    Some(array::from_fn(|index| u32::from_be_bytes(chunks[index])))
}

fn validate_crg_bindings(clocks: [u32; 6], resets: [u32; 15]) -> Result<(), &'static str> {
    if clocks != GPU_CLOCK_BINDINGS {
        return Err("unexpected_clock_bindings");
    }
    if resets != GPU_RESET_BINDINGS {
        return Err("unexpected_reset_bindings");
    }
    Ok(())
}

fn inspect_gpu_crg_dt() -> Result<(), &'static str> {
    inspect_gpu_dt()?;
    let tree = DEVICE_TREE.get().ok_or("no_device_tree")?;
    let gpu = tree.find_compatible(&["img,gpu"]).ok_or("no_gpu_node")?;
    let clocks = gpu.property("clocks").ok_or("no_gpu_clocks")?;
    let resets = gpu.property("resets").ok_or("no_gpu_resets")?;
    validate_crg_bindings(
        decode_cells(clocks.value).ok_or("unexpected_clock_bindings")?,
        decode_cells(resets.value).ok_or("unexpected_reset_bindings")?,
    )?;
    if gpu.property("clock-names").map(|property| property.value)
        != Some(b"aclk\0gray_clk\0cfg_clk\0".as_slice())
        || gpu.property("reset-names").map(|property| property.value)
            != Some(b"axi\0cfg\0gray\0jones\0spu\0".as_slice())
    {
        return Err("unexpected_resource_names");
    }
    let clock_provider = tree.find_phandle(3).ok_or("no_clock_provider")?;
    let reset_provider = tree.find_phandle(20).ok_or("no_reset_provider")?;
    if !clock_provider
        .compatible()
        .is_some_and(|values| values.all().any(|value| value == "eswin,eic7700-clock"))
        || !reset_provider
            .compatible()
            .is_some_and(|values| values.all().any(|value| value == "eswin,eic7700-reset"))
    {
        return Err("unexpected_crg_provider");
    }
    let crg = tree
        .find_node("/soc/sys-crg@51828000")
        .ok_or("no_crg_aperture")?;
    let mut regions = crg.reg().ok_or("no_crg_aperture")?;
    let region = regions.next().ok_or("no_crg_aperture")?;
    if regions.next().is_some()
        || region.starting_address as usize != CRG_BASE
        || region.size != Some(CRG_SIZE)
    {
        return Err("unexpected_crg_aperture");
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct CrgSnapshot {
    aclk: u32,
    cfg: u32,
    gray: u32,
    reset: u32,
}

impl CrgSnapshot {
    fn clock_gates(self) -> u8 {
        u8::from(self.aclk & CRG_GATE_BIT != 0)
            | (u8::from(self.cfg & CRG_GATE_BIT != 0) << 1)
            | (u8::from(self.gray & CRG_GATE_BIT != 0) << 2)
    }

    fn deasserted_resets(self) -> u8 {
        (self.reset & 0x1f) as u8
    }
}

fn read_gpu_crg() -> Result<CrgSnapshot, &'static str> {
    let clocks = IoMem::acquire(CRG_BASE + GPU_ACLK_OFFSET..CRG_BASE + GPU_GRAY_OFFSET + 4)
        .map_err(|_| "clock_registers_unavailable")?;
    let resets = IoMem::acquire(CRG_BASE + GPU_RESET_OFFSET..CRG_BASE + GPU_RESET_OFFSET + 4)
        .map_err(|_| "reset_register_unavailable")?;
    Ok(CrgSnapshot {
        aclk: clocks.read_once(0).map_err(|_| "clock_read_failed")?,
        cfg: clocks
            .read_once(GPU_CFG_OFFSET - GPU_ACLK_OFFSET)
            .map_err(|_| "clock_read_failed")?,
        gray: clocks
            .read_once(GPU_GRAY_OFFSET - GPU_ACLK_OFFSET)
            .map_err(|_| "clock_read_failed")?,
        reset: resets.read_once(0).map_err(|_| "reset_read_failed")?,
    })
}

fn print_gpu_crg_snapshot(snapshot: CrgSnapshot) {
    aster_logger::println!(
        "ASTERINAS_GPU_CRG_PROBE status=observed aclk={:#010x} cfg={:#010x} gray={:#010x} reset={:#010x} gates={:#05b} deasserted={:#07b} crg=read-only gpu_mmio=untouched",
        snapshot.aclk,
        snapshot.cfg,
        snapshot.gray,
        snapshot.reset,
        snapshot.clock_gates(),
        snapshot.deasserted_resets(),
    );
}

pub(super) fn probe_on_request() {
    let requested = |name| {
        boot_info()
            .kernel_cmdline
            .split_whitespace()
            .any(|word| word == name)
    };
    let dt_requested = requested("asterinas.gpu_dt_probe=1");
    let crg_requested = requested("asterinas.gpu_crg_probe=1");
    let power_requested = requested("asterinas.gpu_powered_id_probe=1");
    if !dt_requested && !crg_requested && !power_requested {
        return;
    }
    if dt_requested {
        match inspect_gpu_dt() {
            Ok(()) => {
                aster_logger::println!(
                    "ASTERINAS_GPU_DT_PROBE status=ready base={:#x} size={:#x} clocks=3 resets=5 interrupts=1 dma=noncoherent mmio=untouched power=unverified",
                    GPU_REG_START,
                    GPU_REG_SIZE,
                );
                ostd::info!("PowerVR device-tree resource shape validated without touching MMIO");
            }
            Err(reason) => {
                aster_logger::println!("ASTERINAS_GPU_DT_PROBE status=skipped reason={}", reason);
                ostd::warn!("PowerVR device-tree probe skipped: {}", reason);
            }
        }
    }
    // IoMem acquisitions are not currently recycled on Drop. When both
    // probes are selected, the powered probe must print the snapshot from
    // its own mapping instead of acquiring the CRG range a second time.
    if crg_requested && !power_requested {
        let result = inspect_gpu_crg_dt().and_then(|()| read_gpu_crg());
        match result {
            Ok(snapshot) => print_gpu_crg_snapshot(snapshot),
            Err(reason) => {
                aster_logger::println!("ASTERINAS_GPU_CRG_PROBE status=skipped reason={}", reason)
            }
        }
    }
    if power_requested {
        power::probe_on_request(crg_requested);
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    fn prepared_resources() -> GpuResources<'static> {
        GpuResources {
            base: 0x5140_0000,
            size: Some(0xfffff),
            clocks_bytes: Some(24),
            resets_bytes: Some(60),
            interrupts_bytes: Some(4),
            dma_noncoherent: true,
            status: Some("okay"),
        }
    }

    #[ktest]
    fn prepared_megrez_gpu_resources_are_accepted() {
        assert_eq!(validate_gpu_resources(prepared_resources()), Ok(()));
    }

    #[ktest]
    fn changed_gpu_resources_are_rejected_before_mmio() {
        let mut resources = prepared_resources();
        resources.base = 0x5141_0000;
        assert_eq!(
            validate_gpu_resources(resources),
            Err("unexpected_register_aperture")
        );
        let mut resources = prepared_resources();
        resources.size = Some(0x100000);
        assert_eq!(
            validate_gpu_resources(resources),
            Err("unexpected_register_aperture")
        );
        let mut resources = prepared_resources();
        resources.clocks_bytes = Some(16);
        assert_eq!(
            validate_gpu_resources(resources),
            Err("unexpected_clock_contract")
        );
        let mut resources = prepared_resources();
        resources.resets_bytes = Some(48);
        assert_eq!(
            validate_gpu_resources(resources),
            Err("unexpected_reset_contract")
        );
        let mut resources = prepared_resources();
        resources.interrupts_bytes = None;
        assert_eq!(
            validate_gpu_resources(resources),
            Err("unexpected_interrupt_contract")
        );
        let mut resources = prepared_resources();
        resources.dma_noncoherent = false;
        assert_eq!(
            validate_gpu_resources(resources),
            Err("unexpected_dma_contract")
        );
        let mut resources = prepared_resources();
        resources.status = Some("disabled");
        assert_eq!(validate_gpu_resources(resources), Err("gpu_node_disabled"));
    }

    #[ktest]
    fn qemu_virt_has_no_megrez_gpu_node() {
        assert_eq!(inspect_gpu_dt().err(), Some("no_gpu_node"));
        assert_eq!(inspect_gpu_crg_dt().err(), Some("no_gpu_node"));
    }

    #[ktest]
    fn gpu_crg_bindings_reject_wrong_clock_or_reset() {
        let clocks = [3, 523, 3, 524, 3, 525];
        let resets = [20, 1, 1, 20, 1, 2, 20, 1, 4, 20, 1, 8, 20, 1, 16];
        assert_eq!(validate_crg_bindings(clocks, resets), Ok(()));
        let mut changed_clocks = clocks;
        changed_clocks[3] = 526;
        assert_eq!(
            validate_crg_bindings(changed_clocks, resets),
            Err("unexpected_clock_bindings")
        );
        let mut changed_resets = resets;
        changed_resets[14] = 32;
        assert_eq!(
            validate_crg_bindings(clocks, changed_resets),
            Err("unexpected_reset_bindings")
        );
    }

    #[ktest]
    fn gpu_crg_status_decodes_gate_and_reset_bits() {
        let snapshot = CrgSnapshot {
            aclk: 1 << 31,
            cfg: 0,
            gray: 1 << 31,
            reset: 0b10101,
        };
        assert_eq!(snapshot.clock_gates(), 0b101);
        assert_eq!(snapshot.deasserted_resets(), 0b10101);
    }
}
