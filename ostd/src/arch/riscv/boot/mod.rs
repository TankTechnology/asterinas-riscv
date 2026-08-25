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

pub(super) const BOOT_SATP_MODE: usize = if cfg!(feature = "riscv_sv39_mode") {
    8 << 60
} else {
    9 << 60
};

global_asm!(
    include_str!("bsp_boot.S"),
    SATP_MODE = const BOOT_SATP_MODE,
);

/// The Flattened Device Tree of the platform.
pub static DEVICE_TREE: Once<Fdt> = Once::new();

fn parse_bootloader_name() -> &'static str {
    "Unknown"
}

fn parse_kernel_commandline() -> &'static str {
    DEVICE_TREE.get().unwrap().chosen().bootargs().unwrap_or("")
}

fn parse_initramfs() -> Option<&'static [u8]> {
    let (start, end) = parse_initramfs_range(DEVICE_TREE.get().unwrap())?;

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
    if let Some((start, end)) = parse_initramfs_range(DEVICE_TREE.get().unwrap()) {
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

fn parse_initramfs_range(fdt: &Fdt) -> Option<(usize, usize)> {
    let chosen = fdt
        .find_node("/chosen")
        .expect("[DTB] '/chosen' node is required");
    let start = chosen.property("linux,initrd-start");
    let end = chosen.property("linux,initrd-end");

    let (start, end) = match (start, end) {
        (None, None) => return None,
        (Some(start), Some(end)) => (
            start
                .as_usize()
                .expect("[DTB] 'linux,initrd-start' is malformed"),
            end.as_usize()
                .expect("[DTB] 'linux,initrd-end' is malformed"),
        ),
        _ => panic!("[DTB] initramfs requires both 'linux,initrd-start' and 'linux,initrd-end'"),
    };

    if start >= end {
        panic!("[DTB] initramfs range [{start:#x}, {end:#x}) is empty or reversed");
    }
    Some((start, end))
}

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

    let mut hart_count = 0usize;
    let mut bootstrap_hart_found = false;

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

        let occurrences = cpus
            .children()
            .filter(|node| node.name.split('@').next() == Some("cpu"))
            .filter(|node| {
                node.property("device_type").and_then(|prop| prop.as_str()) == Some("cpu")
                    && node.property("mmu-type").is_some()
            })
            .filter_map(|node| node.property("reg").and_then(|reg| reg.as_usize()))
            .filter(|candidate| *candidate == hart_id)
            .take(2)
            .count();
        if occurrences > 1 {
            panic!("[DTB] duplicate hart ID {hart_id:#x} in '/cpus'");
        }
        hart_count += 1;
        bootstrap_hart_found |= hart_id == bootstrap_hart_id;
    }

    if hart_count == 0 {
        panic!("[DTB] '/cpus' describes no MMU-capable CPU");
    }
    if !bootstrap_hart_found {
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

    if let Some((start, end)) = parse_initramfs_range(fdt)
        && !is_covered_by_memory(fdt, start, end)
    {
        panic!(
            "[DTB] initramfs [{start:#x}, {end:#x}) is outside the RAM declared by '/memory' \
             (device tree '-m' does not match the boot arguments?)"
        );
    }
}

/// Returns whether `[start, end)` is covered by the union of `/memory` regions.
fn is_covered_by_memory(fdt: &Fdt, start: usize, end: usize) -> bool {
    if start >= end {
        return false;
    }

    let mut covered_end = start;
    loop {
        let memory = fdt.memory();
        let next_end = extend_covered_end(
            covered_end,
            memory.regions().filter_map(|region| {
                region
                    .size
                    .map(|size| (region.starting_address as usize, size))
            }),
        );

        if next_end >= end {
            return true;
        }
        if next_end == covered_end {
            return false;
        }
        covered_end = next_end;
    }
}

/// Returns whether a nonempty range is covered by overlapping or adjacent
/// non-overflowing regions.
#[cfg(ktest)]
fn is_covered_by_ranges(start: usize, end: usize, regions: &[(usize, usize)]) -> bool {
    if start >= end {
        return false;
    }

    let mut covered_end = start;
    loop {
        let next_end = extend_covered_end(covered_end, regions.iter().copied());

        if next_end >= end {
            return true;
        }
        if next_end == covered_end {
            return false;
        }
        covered_end = next_end;
    }
}

fn extend_covered_end(covered_end: usize, regions: impl Iterator<Item = (usize, usize)>) -> usize {
    let mut next_end = covered_end;
    for (region_start, region_size) in regions {
        let Some(region_end) = region_start.checked_add(region_size) else {
            continue;
        };
        if region_start <= covered_end && region_end > next_end {
            next_end = region_end;
        }
    }
    next_end
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

/// Checks that every MMU-capable CPU carries a uniquely-addressed local
/// interrupt controller and has a supervisor-external context in a supported
/// PLIC.
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

        let mut controllers = cpu.children().filter(|child| {
            child
                .compatible()
                .is_some_and(|compatible| compatible.all().any(|s| s == "riscv,cpu-intc"))
        });
        let controller = controllers.next().unwrap_or_else(|| {
            panic!(
                "[DTB] CPU node '{}' has no 'riscv,cpu-intc' interrupt controller",
                cpu.name
            )
        });
        if controllers.next().is_some() {
            panic!(
                "[DTB] CPU node '{}' has multiple 'riscv,cpu-intc' controllers",
                cpu.name
            );
        }
        let phandle = controller
            .property("phandle")
            .and_then(|property| property.as_usize())
            .and_then(|value| u32::try_from(value).ok())
            .unwrap_or_else(|| {
                panic!(
                    "[DTB] CPU node '{}' has no valid interrupt-controller phandle",
                    cpu.name
                )
            });

        let phandle_occurrences = cpus
            .children()
            .filter(|node| node.name.split('@').next() == Some("cpu"))
            .filter(|node| {
                node.property("device_type").and_then(|prop| prop.as_str()) == Some("cpu")
                    && node.property("mmu-type").is_some()
            })
            .flat_map(|node| node.children())
            .filter(|child| {
                child
                    .compatible()
                    .is_some_and(|compatible| compatible.all().any(|s| s == "riscv,cpu-intc"))
            })
            .filter_map(|child| child.property("phandle").and_then(|prop| prop.as_usize()))
            .filter(|candidate| u32::try_from(*candidate).ok() == Some(phandle))
            .take(2)
            .count();
        if phandle_occurrences > 1 {
            panic!("[DTB] duplicate CPU interrupt-controller phandle {phandle:#x}");
        }

        let wired = fdt
            .all_nodes()
            .filter(|node| is_supported_plic(*node))
            .any(|plic| {
                let contexts = plic.property("interrupts-extended").unwrap_or_else(|| {
                    panic!(
                        "[DTB] PLIC node '{}' has no 'interrupts-extended'",
                        plic.name
                    )
                });
                if contexts.value.len() % 8 != 0 {
                    panic!(
                        "[DTB] PLIC node '{}' has malformed 'interrupts-extended'",
                        plic.name
                    );
                }
                has_supervisor_external_context(contexts.value, phandle)
            });
        if !wired {
            panic!(
                "[DTB] CPU node '{}' has no PLIC supervisor-external context",
                cpu.name
            );
        }
    }
}

const SUPPORTED_PLIC_COMPATIBLES: [&str; 4] = [
    "andestech,nceplic100",
    "sifive,plic-1.0.0",
    "thead,c900-plic",
    "riscv,plic0",
];

fn is_supported_plic(node: fdt::node::FdtNode<'_, '_>) -> bool {
    node.compatible().is_some_and(|compatible| {
        compatible
            .all()
            .any(|value| SUPPORTED_PLIC_COMPATIBLES.contains(&value))
    })
}

fn has_supervisor_external_context(contexts: &[u8], phandle: u32) -> bool {
    let (contexts, remainder) = contexts.as_chunks::<8>();
    remainder.is_empty()
        && contexts.iter().any(|context| {
            u32::from_be_bytes(context[0..4].try_into().unwrap()) == phandle
                && u32::from_be_bytes(context[4..8].try_into().unwrap()) == 9
        })
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
    use super::{contains_range, has_supervisor_external_context, is_covered_by_ranges};
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

    #[ktest]
    fn range_coverage_accepts_adjacent_regions_and_rejects_gaps() {
        let adjacent = [(0x8000, 0x1000), (0x9000, 0x1000)];
        assert!(is_covered_by_ranges(0x8800, 0x9800, &adjacent));

        let with_gap = [(0x8000, 0x800), (0x9000, 0x1000)];
        assert!(!is_covered_by_ranges(0x8400, 0x9800, &with_gap));
    }

    #[ktest]
    fn plic_context_requires_matching_phandle_and_supervisor_irq() {
        let contexts = [
            0, 0, 0, 1, 0, 0, 0, 11, // hart 1 machine-external
            0, 0, 0, 1, 0, 0, 0, 9, // hart 1 supervisor-external
        ];
        assert!(has_supervisor_external_context(&contexts, 1));
        assert!(!has_supervisor_external_context(&contexts, 2));
        assert!(!has_supervisor_external_context(&contexts[..15], 1));
    }
}
