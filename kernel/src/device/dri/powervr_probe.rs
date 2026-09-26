// SPDX-License-Identifier: MPL-2.0

//! Opt-in, device-tree-only PowerVR resource discovery for Megrez.

use ostd::{arch::boot::DEVICE_TREE, boot::boot_info};

// Pinned to the prepared Megrez DTB. The GPU must not be touched until its
// clock, reset, and power-domain ownership has been established separately.
const GPU_REG_START: usize = 0x5140_0000;
const GPU_REG_SIZE: usize = 0x0f_ffff;
const GPU_CLOCK_CELLS_BYTES: usize = 3 * 2 * 4;
const GPU_RESET_CELLS_BYTES: usize = 5 * 3 * 4;
const GPU_INTERRUPT_CELLS_BYTES: usize = 4;

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

pub(super) fn probe_on_request() {
    if !boot_info()
        .kernel_cmdline
        .split_whitespace()
        .any(|word| word == "asterinas.gpu_dt_probe=1")
    {
        return;
    }

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
    }
}
