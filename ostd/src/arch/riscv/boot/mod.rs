// SPDX-License-Identifier: MPL-2.0

//! The RISC-V boot module defines the entrypoints of Asterinas.

mod simple_framebuffer;
pub(crate) mod smp;

use core::arch::global_asm;

use fdt::Fdt;
use spin::Once;

use crate::{
    boot::{
        BootloaderAcpiArg, BootloaderFramebufferArg,
        memory_region::{MemoryRegion, MemoryRegionArray, MemoryRegionType},
    },
    early_println,
    mm::paddr_to_vaddr,
};

global_asm!(include_str!("bsp_boot.S"));

/// The Flattened Device Tree of the platform.
pub static DEVICE_TREE: Once<Fdt> = Once::new();

fn parse_bootloader_name() -> &'static str {
    "Unknown"
}

fn parse_kernel_commandline() -> &'static str {
    DEVICE_TREE.get().unwrap().chosen().bootargs().unwrap_or("")
}

fn parse_initramfs() -> Option<&'static [u8]> {
    let (start, end) = parse_initramfs_range()?;

    let base_va = paddr_to_vaddr(start);
    let length = end - start;
    Some(unsafe { core::slice::from_raw_parts(base_va as *const u8, length) })
}

fn parse_acpi_arg() -> BootloaderAcpiArg {
    // TDDO: Add ACPI support for RISC-V, maybe.
    BootloaderAcpiArg::NotProvided
}

fn parse_framebuffer_info() -> Option<BootloaderFramebufferArg> {
    simple_framebuffer::parse(DEVICE_TREE.get().unwrap())
}

fn parse_memory_regions() -> MemoryRegionArray {
    let mut regions = MemoryRegionArray::new();

    for region in DEVICE_TREE.get().unwrap().memory().regions() {
        if region.size.unwrap_or(0) > 0 {
            regions
                .push(MemoryRegion::new(
                    region.starting_address as usize,
                    region.size.unwrap(),
                    MemoryRegionType::Usable,
                ))
                .unwrap();
        }
    }

    if let Some(node) = DEVICE_TREE.get().unwrap().find_node("/reserved-memory") {
        for child in node.children() {
            if let Some(reg_iter) = child.reg() {
                for region in reg_iter {
                    regions
                        .push(MemoryRegion::new(
                            region.starting_address as usize,
                            region.size.unwrap(),
                            MemoryRegionType::Reserved,
                        ))
                        .unwrap();
                }
            }
        }
    }

    // Add the kernel region.
    regions.push(MemoryRegion::kernel()).unwrap();

    // Add the initramfs region.
    if let Some((start, end)) = parse_initramfs_range() {
        regions
            .push(MemoryRegion::new(
                start,
                end - start,
                MemoryRegionType::Module,
            ))
            .unwrap();
    }

    // Keep the firmware scanout buffer out of the physical frame allocator.
    if let Some(framebuffer) = parse_framebuffer_info() {
        regions
            .push(MemoryRegion::framebuffer(&framebuffer))
            .unwrap();
    }

    regions.into_non_overlapping()
}

fn parse_initramfs_range() -> Option<(usize, usize)> {
    let chosen = DEVICE_TREE.get().unwrap().find_node("/chosen").unwrap();
    let initrd_start = chosen.property("linux,initrd-start")?.as_usize()?;
    let initrd_end = chosen.property("linux,initrd-end")?.as_usize()?;
    Some((initrd_start, initrd_end))
}

/// The maximum number of harts we are willing to check in a device tree.
///
/// This only sizes a fixed, stack-allocated scratch buffer during early boot
/// (before the heap allocator is available), so the exact bound is not critical.
const MAX_DT_HARTS: usize = 256;

/// Validates that the device tree is internally consistent before the kernel
/// starts trusting its CPU count, memory layout, and interrupt wiring.
///
/// A device tree generated with flags that do not match the ones used to boot
/// the guest (e.g. `-machine dumpdtb` without `-m 2G`, or a stale `-smp`) is
/// self-consistent on its own and therefore passes every downstream parser, yet
/// contradicts the platform it is booted on. The resulting mismatch corrupts
/// memory or hangs the kernel silently instead of failing cleanly. These checks
/// turn the most damaging cases into an early, descriptive panic.
fn validate_device_tree(fdt: &Fdt, bootstrap_hart_id: usize) {
    validate_cpu_nodes(fdt, bootstrap_hart_id);
    validate_memory_layout(fdt);
    validate_interrupt_controllers(fdt);
}

/// Checks that `/cpus` describes at least one MMU-capable, uniquely-addressed
/// hart and that it includes the hart the bootloader started us on.
///
/// The kernel enumerates application harts with the same criteria used by
/// `smp::for_each_hart_id`: `device_type == "cpu"`, a `mmu-type` property, and
/// a parseable `reg`. If the device tree does not describe the bootstrapping
/// hart, SMP bring-up starts the wrong set of harts and can spin forever.
fn validate_cpu_nodes(fdt: &Fdt, bootstrap_hart_id: usize) {
    let cpus = fdt
        .find_node("/cpus")
        .expect("[DTB] '/cpus' node is required");

    let mut hart_ids = [0usize; MAX_DT_HARTS];
    let mut hart_count = 0usize;

    for cpu in cpus
        .children()
        .filter(|node| node.name.split('@').next() == Some("cpu"))
    {
        if cpu.property("device_type").and_then(|prop| prop.as_str()) != Some("cpu") {
            continue;
        }
        if cpu.property("mmu-type").is_none() {
            // Management or monitor harts without an MMU are skipped by the
            // kernel as well, so they are not checked here.
            continue;
        }

        let Some(hart_id) = cpu.property("reg").and_then(|reg| reg.as_usize()) else {
            panic!(
                "[DTB] CPU node '{}' has no parseable 'reg' property",
                cpu.name
            );
        };

        if hart_count == MAX_DT_HARTS {
            panic!("[DTB] too many CPU nodes (more than {MAX_DT_HARTS})");
        }
        if hart_ids[..hart_count].contains(&hart_id) {
            panic!("[DTB] duplicate hart ID {hart_id:#x} in '/cpus'");
        }
        hart_ids[hart_count] = hart_id;
        hart_count += 1;
    }

    if hart_count == 0 {
        panic!("[DTB] '/cpus' describes no MMU-capable CPU");
    }
    if !hart_ids[..hart_count].contains(&bootstrap_hart_id) {
        panic!(
            "[DTB] bootstrap hart {bootstrap_hart_id:#x} is not described under '/cpus' \
             (device tree '-smp' does not match the boot arguments?)"
        );
    }
}

/// Checks that `/memory` declares usable RAM and that the kernel image and the
/// initramfs are both covered by it.
///
/// The kernel derives the linear-mapping top (`max_paddr`) and the frame
/// metadata from the `/memory` node, independently of the frame allocator's
/// usable regions. A device tree generated without the boot-time `-m` (e.g.
/// 128 MiB while the guest is booted with 2 GiB) places the initramfs beyond
/// the declared RAM; the kernel then silently corrupts memory while building
/// the page tables instead of failing. Fail fast with a descriptive panic.
fn validate_memory_layout(fdt: &Fdt) {
    if fdt
        .memory()
        .regions()
        .all(|region| region.size.unwrap_or(0) == 0)
    {
        panic!("[DTB] '/memory' node declares no usable RAM");
    }

    let kernel = MemoryRegion::kernel();
    if !is_covered_by_memory(fdt, kernel.base(), kernel.end()) {
        panic!(
            "[DTB] kernel image [{:#x}, {:#x}) is outside the RAM declared by '/memory' \
             (device tree '-m' does not match the boot arguments?)",
            kernel.base(),
            kernel.end()
        );
    }

    if let Some((start, end)) = parse_initramfs_range()
        && !is_covered_by_memory(fdt, start, end)
    {
        panic!(
            "[DTB] initramfs [{start:#x}, {end:#x}) is outside the RAM declared by '/memory' \
             (device tree '-m' does not match the boot arguments?)"
        );
    }
}

/// Returns whether `[start, end)` is entirely contained in a `/memory` region.
fn is_covered_by_memory(fdt: &Fdt, start: usize, end: usize) -> bool {
    fdt.memory().regions().any(|region| {
        let Some(size) = region.size else {
            return false;
        };
        let base = region.starting_address as usize;
        contains_range(base, size, start, end)
    })
}

/// Returns whether a nonempty range is covered by a non-overflowing region.
fn contains_range(region_start: usize, region_size: usize, start: usize, end: usize) -> bool {
    if start >= end {
        return false;
    }

    let Some(region_end) = region_start.checked_add(region_size) else {
        return false;
    };
    start >= region_start && end <= region_end
}

/// Checks that every MMU-capable CPU carries a local interrupt controller
/// (`riscv,cpu-intc`) with a `phandle`.
///
/// The PLIC driver uses that phandle to map `interrupts-extended` entries back
/// to harts; a CPU missing it is silently left without external interrupts.
fn validate_interrupt_controllers(fdt: &Fdt) {
    let cpus = fdt
        .find_node("/cpus")
        .expect("[DTB] '/cpus' node is required");

    for cpu in cpus
        .children()
        .filter(|node| node.name.split('@').next() == Some("cpu"))
    {
        if cpu.property("device_type").and_then(|prop| prop.as_str()) != Some("cpu") {
            continue;
        }
        if cpu.property("mmu-type").is_none() {
            continue;
        }

        let wired = cpu.children().any(|child| {
            child
                .compatible()
                .is_some_and(|compatible| compatible.all().any(|s| s == "riscv,cpu-intc"))
                && child.property("phandle").is_some()
        });
        if !wired {
            panic!(
                "[DTB] CPU node '{}' has no 'riscv,cpu-intc' interrupt controller with a phandle",
                cpu.name
            );
        }
    }
}

static mut BOOTSTRAP_HART_ID: u32 = u32::MAX;

/// The entry point of the Rust code portion of Asterinas.
///
/// `BOOTSTRAP_HART_ID` is initialized to be `hart_id` and accessible after calling this.
///
/// # Safety
///
/// - This function must be called only once at a proper timing in the BSP's boot assembly code.
/// - The caller must follow C calling conventions and put the right arguments in registers.
// SAFETY: The name does not collide with other symbols.
#[unsafe(no_mangle)]
unsafe extern "C" fn riscv_boot(hart_id: usize, device_tree_paddr: usize) -> ! {
    early_println!("Enter riscv_boot");

    // We will only write it once. Other processors will only read it.
    // SAFETY: We don't create Rust references, so there are no aliasing problems. Other processors
    // have not been booted yet, so there are no data races.
    unsafe { BOOTSTRAP_HART_ID = hart_id as u32 };

    let device_tree_ptr = paddr_to_vaddr(device_tree_paddr) as *const u8;
    let fdt = unsafe { Fdt::from_ptr(device_tree_ptr).unwrap() };
    DEVICE_TREE.call_once(|| fdt);

    // The device tree is the bootloader's contract with the kernel. Validate it
    // before trusting its CPU count, memory layout, and interrupt wiring, so a
    // mismatched device tree panics with a clear message instead of corrupting
    // memory or hanging silently.
    validate_device_tree(DEVICE_TREE.get().unwrap(), hart_id);

    use crate::boot::{EARLY_INFO, EarlyBootInfo, start_kernel};

    EARLY_INFO.call_once(|| EarlyBootInfo {
        bootloader_name: parse_bootloader_name(),
        kernel_cmdline: parse_kernel_commandline(),
        initramfs: parse_initramfs(),
        acpi_arg: parse_acpi_arg(),
        framebuffer_arg: parse_framebuffer_info(),
        memory_regions: parse_memory_regions(),
    });

    // SAFETY: The safety is guaranteed by the safety preconditions and the fact that we call it
    // once after setting up necessary resources.
    unsafe { start_kernel() };
}

#[cfg(ktest)]
mod tests {
    use super::contains_range;
    use crate::prelude::ktest;

    #[ktest]
    fn range_containment_rejects_invalid_and_overflowing_regions() {
        assert!(contains_range(0x8000, 0x2000, 0x9000, 0xa000));
        assert!(!contains_range(0x8000, 0x2000, 0xa000, 0x9000));
        assert!(!contains_range(
            usize::MAX - 0xfff,
            0x2000,
            usize::MAX - 0x800,
            usize::MAX,
        ));
    }
}
