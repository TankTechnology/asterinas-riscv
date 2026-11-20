// SPDX-License-Identifier: MPL-2.0

//! A read-only, third-party view of another hart's machine-level timer and
//! inter-processor-interrupt registers.
//!
//! Every other instrument observes a hart from inside that hart: a marker it
//! writes, a trap it takes, a counter it bumps. A hart that stops executing --
//! in particular one that entered machine mode and never returned -- writes
//! none of those, and looks identical to a hart parked in a long critical
//! section. The ACLINT block is ordinary memory-mapped I/O, so any hart can
//! read which other harts have their `MSIP` set (an interrupt was raised and
//! not yet taken) and where their `mtimecmp` stands (whether the timer
//! deadline is in the past and still not handled).
//!
//! Nothing here writes, takes a lock, or changes any behavior. If the windows
//! cannot be mapped, the probe says so once and every later read returns
//! `None`; the rest of the kernel is unaffected.
//!
//! # Indexing
//!
//! The platform indexes both arrays by a hart's *position in the node's
//! `interrupts-extended` list*, not by its hardware hart id (OpenSBI does
//! `msip[hartid - first_hartid]`). On a tree that lists all harts in order
//! the two agree, but nothing guarantees that, and a wrong index reads a
//! different hart and looks completely normal. So the order is parsed here
//! and the hardware hart id is mapped to its position explicitly.
//!
//! `mtime` is one global counter, not a per-hart array; it is read through
//! the `time` CSR, which is the same counter, instead of through MMIO.

use alloc::vec::Vec;
use core::sync::atomic::{AtomicBool, Ordering};

use fdt::{Fdt, node::FdtNode};
use spin::Once;

use crate::{arch::boot::DEVICE_TREE, early_println, io::IoMem, mm::io::VmIoOnce};

/// Offset of the `mtimecmp` array inside a combined SiFive CLINT window.
const CLINT_MTIMECMP_OFFSET: usize = 0x4000;

/// The EIC7700's ACLINT window, from the vendor platform source
/// (`eic770x/platform.c`: `EIC770X_ACLINT_MSWI_ADDR = 0x2000000`,
/// `mtimecmp_addr = +0x4000`, `first_hartid = 0`).
const EIC7700_MSWI_BASE: usize = 0x0200_0000;
const EIC7700_MTIMECMP_BASE: usize = 0x0200_4000;

static LAYOUT: Once<Layout> = Once::new();
static MAPPED: AtomicBool = AtomicBool::new(false);
/// Whether the kernel is allowed to read the window at all.
///
/// On Megrez the answer from the firmware is "no": a supervisor load raises
/// `LoadFault` (PMP keeps the ACLINT window machine-mode only). Every read
/// in this module sits behind this flag, because a read on the *stall*
/// report path would panic exactly when the report is being built and erase
/// the evidence it was meant to carry.
static READ_ENABLED: AtomicBool = AtomicBool::new(false);

struct Layout {
    registers: IoMem,
    /// Absolute address of the `MSIP` array.
    msip_base: usize,
    /// Absolute address of the `mtimecmp` array.
    mtimecmp_base: usize,
    /// Hardware hart id at each array index, as the device tree lists them.
    msip_order: Vec<u32>,
    mtimecmp_order: Vec<u32>,
}

/// One hart's machine-level state, read by another hart.
#[derive(Clone, Copy)]
pub struct HartState {
    /// The hart's `MSIP` register: bit 0 means an IPI is pending.
    pub msip: u32,
    /// The shared `mtime` counter at the moment of the read.
    pub mtime: u64,
    /// The hart's own `mtimecmp` value.
    pub mtimecmp: u64,
}

/// What changed over a short window on one hart, read from outside.
#[derive(Clone, Copy)]
pub struct HartSnapshot {
    /// `MSIP` at the first sample.
    pub msip: u32,
    /// `MSIP` at the second sample.
    pub msip_after: u32,
    /// The array index used for `MSIP`, and for `mtimecmp`. They differ only
    /// if the two nodes list the harts in different orders, and printing them
    /// makes a mis-indexed read impossible to mistake for a real one.
    pub msip_index: usize,
    pub mtimecmp_index: usize,
    /// `mtime - mtimecmp` at the first sample, in microseconds. Positive
    /// means the timer deadline was already in the past.
    pub lag_us: i64,
    /// How much that lag changed over the window. About `+gap_us` means the
    /// hart never re-armed its timer; near zero or negative means it did.
    pub lag_delta_us: i64,
}

fn cell(bytes: &[u8], index: usize) -> Option<u32> {
    let start = index * 4;
    let bytes = bytes.get(start..start + 4)?;
    Some(u32::from_be_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]))
}

fn compatible_has(node: &FdtNode, name: &str) -> bool {
    node.compatible()
        .is_some_and(|list| list.all().any(|item| item == name))
}

/// The hart ids in the order the node's `interrupts-extended` lists them.
///
/// Each entry is `(phandle, interrupt specifier)`; the phandle points at a
/// `/cpus/cpu@N` node whose `reg` is the hardware hart id. A node without
/// that property falls back to the identity order, which is what every
/// tree observed so far actually uses.
/// The hart ids covered by `node`, in the order that indexes its registers.
///
/// Each `interrupts-extended` entry is `(phandle, interrupt specifier)`, and
/// the phandle points at a `/cpus/cpu@N` node whose `reg` is the hardware
/// hart id. A combined SiFive CLINT node lists *two* entries per hart (3 for
/// the software interrupt, 7 for the timer), so the list position is not the
/// array index there: only entries carrying `irq` count, and a hart listed
/// twice counts once. The resulting order is the array's indexing order;
/// when it is contiguous and ascending, a hart's position equals
/// `hartid - first_hartid`, which is what the firmware itself assumes.
fn hart_order(tree: &Fdt, node: &FdtNode, irq: u32) -> Vec<u32> {
    let Some(property) = node.property("interrupts-extended") else {
        return Vec::new();
    };
    let value = property.value;
    let mut order = Vec::new();
    for index in 0..(value.len() / 8) {
        let Some(phandle) = cell(value, index * 2) else {
            break;
        };
        let specifier = cell(value, index * 2 + 1).unwrap_or(u32::MAX);
        if specifier != irq {
            continue;
        }
        let Some(cpu) = tree.find_phandle(phandle) else {
            continue;
        };
        let Some(reg) = cpu.property("reg") else {
            continue;
        };
        if let Some(hart) = cell(reg.value, 0)
            && !order.contains(&hart)
        {
            order.push(hart);
        }
    }
    order
}

/// Finds the ACLINT/CLINT windows in the device tree and maps them read-only.
///
/// Called once on the BSP once the frame allocator and the direct map are up.
/// Every address comes from the tree the kernel is booting with, so the
/// board's layout (or a QEMU run) is described, not assumed.
pub fn probe() {
    // `asterinas.clint_read=1` turns on the one read that the firmware's PMP
    // makes fatal: on this board a supervisor load from the ACLINT window is
    // a load access fault, so the probe is off by default and the boot-time
    // read is an opt-in experiment. Without the knob the probe still prints
    // where the window is and which source the address came from, which is
    // the part that is useful on every boot.
    let read_enabled = crate::boot::EARLY_INFO.get().is_some_and(|info| {
        info.kernel_cmdline
            .split_ascii_whitespace()
            .any(|arg| arg == "asterinas.clint_read=1")
    });
    READ_ENABLED.store(read_enabled, Ordering::Release);
    let tree = DEVICE_TREE.get();
    let combined = ["riscv,clint0", "sifive,clint0"];
    let mswi_node = tree
        .and_then(|tree| tree.find_compatible(&["riscv,aclint-mswi", combined[0], combined[1]]));
    let mtimer_node = tree
        .and_then(|tree| tree.find_compatible(&["riscv,aclint-mtimer", combined[0], combined[1]]));

    // The Megrez device tree has no CLINT node at all: the firmware's own
    // platform code hard-codes the window, so the board's S-mode tree never
    // describes it. The addresses below are from the vendor's EIC7700
    // platform source (`eic770x/platform.c`), where `first_hartid = 0`, so
    // the array index is the hardware hart id. Using them says "the tree did
    // not describe the window", not "the window is inaccessible"; the two
    // are reported separately.
    let (msip_base, mtimecmp_base, msip_order, mtimecmp_order, source) =
        match (tree, mswi_node, mtimer_node) {
            (Some(tree), Some(mswi_node), Some(mtimer_node)) => {
                let Some(mswi_region) = mswi_node.reg().and_then(|mut regions| regions.next())
                else {
                    early_println!("ASTERINAS_TIMER_MMIO status=skip reason=no-mswi-reg");
                    return;
                };
                let Some(mtimer_region) = mtimer_node.reg().and_then(|mut regions| regions.next())
                else {
                    early_println!("ASTERINAS_TIMER_MMIO status=skip reason=no-mtimer-reg");
                    return;
                };
                let msip_base = mswi_region.starting_address as usize;
                // A node of its own starts at `mtimecmp`; a combined SiFive
                // CLINT window keeps it 0x4000 bytes into the same window as
                // `MSIP`.
                let mtimecmp_base = if compatible_has(&mtimer_node, "riscv,aclint-mtimer") {
                    mtimer_region.starting_address as usize
                } else {
                    mtimer_region.starting_address as usize + CLINT_MTIMECMP_OFFSET
                };
                (
                    msip_base,
                    mtimecmp_base,
                    hart_order(tree, &mswi_node, 3),
                    hart_order(tree, &mtimer_node, 7),
                    "dtb",
                )
            }
            _ => {
                let harts = crate::cpu::num_cpus().max(1) as u32;
                let order = (0..harts).collect::<Vec<_>>();
                (
                    EIC7700_MSWI_BASE,
                    EIC7700_MTIMECMP_BASE,
                    order.clone(),
                    order,
                    "eic7700-const",
                )
            }
        };

    let harts = msip_order.len().max(mtimecmp_order.len()).max(1);
    let start = msip_base.min(mtimecmp_base);
    let end = (msip_base + 4 * harts).max(mtimecmp_base + 8 * harts);
    let Ok(registers) = IoMem::acquire(start..end) else {
        early_println!(
            "ASTERINAS_TIMER_MMIO status=skip reason=mmio-unavailable source={} msip_base={:#x} mtimer_base={:#x}",
            source,
            msip_base,
            mtimecmp_base
        );
        return;
    };
    let layout = Layout {
        registers,
        msip_base,
        mtimecmp_base,
        msip_order,
        mtimecmp_order,
    };
    early_println!(
        "ASTERINAS_TIMER_MMIO status=ok source={} mswi_base={:#x} mswi_first_hartid={} mtimecmp_base={:#x} mtime_base=time-csr n={} mswi_order={:?} mtimer_order={:?}",
        source,
        msip_base,
        layout.msip_order.first().copied().unwrap_or(u32::MAX),
        mtimecmp_base,
        layout.msip_order.len(),
        layout.msip_order.as_slice(),
        layout.mtimecmp_order.as_slice()
    );
    MAPPED.store(true, Ordering::Release);
    let _ = LAYOUT.call_once(|| {
        // `IoMem` is not `Clone`; move it in and keep the addresses.
        layout
    });
    // Read once now, on purpose. If the firmware's PMP keeps this window
    // machine-mode-only, the load faults and the kernel panics here -- at
    // boot, where the message is immediate -- instead of burning a whole
    // round waiting for a stall report that can never read anything. These
    // are the *first* reads of the window; a fault here is the answer.
    //
    // Behind `asterinas.clint_read=1` because on Megrez the answer was "yes,
    // it faults": a supervisor load from the ACLINT window raises
    // `LoadFault` (PMP keeps it machine-mode only). A boot that panics by
    // default would make every other measurement impossible, so the
    // experiment is opt-in and the address/source line still prints on
    // every boot.
    if read_enabled {
        let self_hart = crate::arch::boot::smp::get_current_hart_id();
        early_println!(
            "ASTERINAS_TIMER_MMIO status=read self_hwid={} msip={} mtimecmp={}",
            self_hart,
            hart_state(self_hart).map_or(
                alloc::string::String::from("failed"),
                |state| alloc::format!("{:#x}", state.msip)
            ),
            hart_state(self_hart).map_or(
                alloc::string::String::from("failed"),
                |state| alloc::format!("{:#x}", state.mtimecmp)
            )
        );
    }
}

/// Whether the probe mapped the windows.
pub fn is_mapped() -> bool {
    MAPPED.load(Ordering::Acquire)
}

/// Whether reading the window is allowed (`asterinas.clint_read=1`).
///
/// False by default: on this board a supervisor read is a fatal load fault,
/// and the stall report is the worst possible place to discover that -- the
/// panic replaces the very line it was building.
pub fn is_read_enabled() -> bool {
    READ_ENABLED.load(Ordering::Acquire)
}

/// The array index of `hart` in `order`, or the hardware id when the node
/// listed no order at all (all trees seen so far list every hart in order).
fn index_of(order: &[u32], hart: u32) -> Option<usize> {
    if order.is_empty() {
        return Some(hart as usize);
    }
    order.iter().position(|&entry| entry == hart)
}

fn read_msip(layout: &Layout, hart: u32) -> Option<u32> {
    let index = index_of(&layout.msip_order, hart)?;
    layout
        .registers
        .read_once::<u32>(layout.msip_base - window_start(layout) + 4 * index)
        .ok()
}

fn read_mtimecmp(layout: &Layout, hart: u32) -> Option<u64> {
    let index = index_of(&layout.mtimecmp_order, hart)?;
    let offset = layout.mtimecmp_base - window_start(layout) + 8 * index;
    // The block has no 64-bit MMIO (`has_64bit_mmio = false` in the vendor
    // platform code), so the halves are read separately. Read the high half
    // on both sides of the low half and accept only a stable pair: `mtimecmp`
    // is rewritten by the target, so a torn read is possible.
    for _ in 0..4 {
        let hi_before = layout.registers.read_once::<u32>(offset + 4).ok()? as u64;
        let lo = layout.registers.read_once::<u32>(offset).ok()? as u64;
        let hi_after = layout.registers.read_once::<u32>(offset + 4).ok()? as u64;
        if hi_before == hi_after {
            return Some((hi_before << 32) | lo);
        }
    }
    None
}

/// The window start the mapped `IoMem` begins at: the smaller of the two
/// arrays' addresses.
fn window_start(layout: &Layout) -> usize {
    layout.msip_base.min(layout.mtimecmp_base)
}

/// Reads `hart`'s `MSIP`, its `mtimecmp`, and the shared `mtime`.
///
/// `None` means the probe never mapped the windows (or a read failed); it is
/// never an error the caller has to handle further.
pub fn hart_state(hart: u32) -> Option<HartState> {
    let layout = LAYOUT.get()?;
    Some(HartState {
        msip: read_msip(layout, hart)?,
        mtime: crate::arch::read_tsc(),
        mtimecmp: read_mtimecmp(layout, hart)?,
    })
}

/// Two samples of `hart`'s machine state, `gap_us` apart.
///
/// One sample cannot distinguish "this hart stopped re-arming its timer" from
/// "the timer is fine and something else is wrong": both show an `mtimecmp`
/// value, and only its *movement* says which. The gap is spent busy-reading
/// the `time` CSR, not sleeping -- a timer sleep would run through exactly
/// the machinery under study.
pub fn hart_snapshot(hart: u32, gap_us: u64) -> Option<HartSnapshot> {
    let layout = LAYOUT.get()?;
    let frequency = crate::arch::tsc_freq().max(1);
    let first = hart_state(hart)?;
    let gap_ticks = gap_us.saturating_mul(frequency) / 1_000_000;
    let mut spins = 0u32;
    while crate::arch::read_tsc().wrapping_sub(first.mtime) < gap_ticks {
        spins += 1;
        if spins > 5_000_000 {
            // `time` is a free-running platform counter; if it never moved,
            // the CSR read is broken and a second sample means nothing.
            return None;
        }
        core::hint::spin_loop();
    }
    let second = hart_state(hart)?;
    let frequency = i128::from(frequency);
    let lag = |state: HartState| i128::from(state.mtime) - i128::from(state.mtimecmp);
    let us = |ticks: i128| (ticks * 1_000_000 / frequency) as i64;
    Some(HartSnapshot {
        msip: first.msip,
        msip_after: second.msip,
        msip_index: index_of(&layout.msip_order, hart)?,
        mtimecmp_index: index_of(&layout.mtimecmp_order, hart)?,
        lag_us: us(lag(first)),
        lag_delta_us: us(lag(second) - lag(first)),
    })
}
