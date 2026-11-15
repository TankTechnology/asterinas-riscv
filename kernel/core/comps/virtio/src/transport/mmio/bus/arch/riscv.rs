// SPDX-License-Identifier: MPL-2.0

use alloc::vec::Vec;

use fdt_util::{AcquireIoMems, AcquireIrqLines};
use ostd::{arch::boot::DEVICE_TREE, mm::HasPaddr};
pub(super) use ostd::arch::irq::MappedIrqLine;

use crate::transport::mmio::{
    MMIO_BUS, bus::common_device::MmioCommonDevice, layout::VirtioMmioLayout,
};

/// Sorts the pending MMIO slots by their start address.
fn sort_mmio_slots_by_address<T>(slots: &mut [(usize, T)]) {
    slots.sort_unstable_by_key(|(mmio_start, _)| *mmio_start);
}

pub(super) fn probe_for_device() {
    // The device tree parsing logic here assumed a Linux-compatible device
    // tree.
    // Reference: <https://www.kernel.org/doc/Documentation/devicetree/bindings/virtio/mmio.txt>.
    let device_tree = DEVICE_TREE.get().unwrap();
    let mmio_nodes = device_tree.all_nodes().filter(|node| {
        node.compatible().is_some_and(|compatibles| {
            compatibles
                .all()
                .any(|compatible| compatible == "virtio,mmio")
        })
    });
    // QEMU assigns virtio-mmio devices to the fixed slots in ascending MMIO
    // address order (the first `-device` gets the lowest address), but it emits
    // the `virtio,mmio` nodes in the device tree in *descending* address order.
    // Registering the devices in tree order would therefore reverse the guest
    // device numbering: the second `-device virtio-blk` would show up as `vda`
    // and the first (boot) disk as `vdb`. Sort the slots by MMIO address so the
    // kernel enumerates them in the same order QEMU assigns them.
    let mut mmio_slots = Vec::new();
    for node in mmio_nodes {
        let Some([io_mem]) = node.acquire_io_mems([size_of::<VirtioMmioLayout>()]) else {
            continue;
        };
        if super::validate_mmio_device(&io_mem).is_err() {
            continue;
        }

        let Some([irq_line]) = node.acquire_irq_lines() else {
            continue;
        };

        mmio_slots.push((io_mem.paddr(), MmioCommonDevice::new(io_mem, irq_line)));
    }
    sort_mmio_slots_by_address(&mut mmio_slots);

    for (_, device) in mmio_slots {
        MMIO_BUS.lock().register_mmio_device(device);
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::sort_mmio_slots_by_address;

    #[ktest]
    fn sorts_mmio_slots_by_ascending_address() {
        let mut slots = [(0x3000, ()), (0x1000, ()), (0x2000, ())];

        sort_mmio_slots_by_address(&mut slots);

        assert_eq!(slots.map(|slot| slot.0), [0x1000, 0x2000, 0x3000]);
    }
}
