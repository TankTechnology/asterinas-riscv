// SPDX-License-Identifier: MPL-2.0

use alloc::vec::Vec;
use core::ops::Range;

pub(super) use ostd::arch::irq::MappedIrqLine;
use ostd::arch::{
    boot::DEVICE_TREE,
    irq::{IRQ_CHIP, InterruptSourceInFdt},
};

struct MmioSlot {
    range: Range<usize>,
    interrupt_source_in_fdt: InterruptSourceInFdt,
}

fn sort_mmio_slots_by_address(slots: &mut [MmioSlot]) {
    slots.sort_unstable_by_key(|slot| slot.range.start);
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
        let mmio_region = node.reg().unwrap().next().unwrap();
        let mmio_start = mmio_region.starting_address as usize;
        let mmio_end = mmio_start
            .checked_add(mmio_region.size.unwrap())
            .expect("virtio-mmio range must not overflow");

        let interrupt_source_in_fdt = InterruptSourceInFdt {
            interrupt: node.interrupts().unwrap().next().unwrap() as u32,
            interrupt_parent: node
                .property("interrupt-parent")
                .and_then(|prop| prop.as_usize())
                .unwrap() as u32,
        };

        mmio_slots.push(MmioSlot {
            range: mmio_start..mmio_end,
            interrupt_source_in_fdt,
        });
    }
    sort_mmio_slots_by_address(&mut mmio_slots);

    for slot in mmio_slots {
        let _ = super::try_register_mmio_device(slot.range, |irq_line| {
            IRQ_CHIP
                .get()
                .unwrap()
                .map_fdt_pin_to(slot.interrupt_source_in_fdt, irq_line)
        });
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::{InterruptSourceInFdt, MmioSlot, sort_mmio_slots_by_address};

    #[ktest]
    fn sorts_mmio_slots_by_ascending_address() {
        let interrupt_source_in_fdt = InterruptSourceInFdt {
            interrupt_parent: 1,
            interrupt: 1,
        };
        let mut slots = [
            MmioSlot {
                range: 0x3000..0x4000,
                interrupt_source_in_fdt,
            },
            MmioSlot {
                range: 0x1000..0x2000,
                interrupt_source_in_fdt,
            },
            MmioSlot {
                range: 0x2000..0x3000,
                interrupt_source_in_fdt,
            },
        ];

        sort_mmio_slots_by_address(&mut slots);

        assert_eq!(slots.map(|slot| slot.range.start), [0x1000, 0x2000, 0x3000]);
    }
}
