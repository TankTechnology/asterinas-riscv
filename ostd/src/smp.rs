// SPDX-License-Identifier: MPL-2.0

//! Symmetric Multi-Processing (SMP) support.
//!
//! This module provides a way to execute code on other processors via inter-
//! processor interrupts.
//!
//! Callers issue work with [`inter_processor_call`]. Remote calls are queued for
//! interrupt-context execution and return a [`PendingIpis`] handle that can be
//! waited on when the caller needs completion.

use alloc::{boxed::Box, collections::VecDeque};
use core::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicUsize, Ordering};

use spin::Once;

use crate::{
    arch::{irq::HwCpuId, trap::TrapFrame},
    cpu::{CpuSet, PinCurrentCpu},
    cpu_local, irq,
    sync::SpinLock,
    util::id_set::Id,
};

/// Executes a function on other processors.
///
/// The provided function `call_fn` will be executed on all target processors
/// specified by `targets`. It can also be executed on the current processor.
/// The function should be short and non-blocking, as it will be executed in
/// interrupt context with interrupts disabled.
///
/// The function `call_fn` will be executed asynchronously on the target
/// processors. However, if called on the current processor, it will be
/// synchronous.
///
/// The returned [`PendingIpis`] can be used to wait until all remote target
/// processors have handled IPIs for this call.
///
/// # Panics
///
/// This function will panic if a hardware error occurs while sending an IPI to
/// a remote processor.
pub fn inter_processor_call(targets: &CpuSet, call_fn: fn()) -> PendingIpis {
    let ipi_sender = IPI_SENDER.get().unwrap();
    ipi_sender.inter_processor_call(targets, call_fn)
}

/// Sends a bare interrupt to every other CPU, for a load experiment.
///
/// No callback is queued, so the target CPUs take the interrupt, find nothing
/// to do and return. The point is the firmware call on this side: this makes
/// it possible to raise the machine-mode trap rate on its own, with none of
/// the page-table work that normally accompanies it.
///
/// With `one_call`, every target is named in a single firmware call; without
/// it, each target gets its own call, the shape the kernel used before the
/// mask batching. An experiment can therefore hold the number of delivered
/// interrupts fixed and change only the number of machine-mode entries.
///
/// Returns how many CPUs were targeted.
pub fn send_bare_ipi_to_others(one_call: bool) -> usize {
    #[cfg(not(target_arch = "riscv64"))]
    {
        let _ = one_call;
        return 0;
    }
    #[cfg(target_arch = "riscv64")]
    {
        const MAX_TARGETS: usize = 64;
        let Some(mapping) = hw_cpu_id_mapping() else {
            return 0;
        };
        let this_cpu = crate::cpu::CpuId::current_racy().as_usize();
        let mut hw_cpu_ids = [mapping[0]; MAX_TARGETS];
        let mut num_hw_cpu_ids = 0;
        for (cpu, &hw_cpu_id) in mapping.iter().enumerate() {
            if cpu == this_cpu || num_hw_cpu_ids >= MAX_TARGETS {
                continue;
            }
            hw_cpu_ids[num_hw_cpu_ids] = hw_cpu_id;
            num_hw_cpu_ids += 1;
        }
        if num_hw_cpu_ids == 0 {
            return 0;
        }
        let guard = crate::task::disable_preempt();
        if one_call {
            let _ = crate::arch::irq::send_ipi_to_harts(&hw_cpu_ids[..num_hw_cpu_ids], &guard as _);
        } else {
            for &hw_cpu_id in &hw_cpu_ids[..num_hw_cpu_ids] {
                let _ = crate::arch::irq::send_ipi(hw_cpu_id, &guard as _);
            }
        }
        num_hw_cpu_ids
    }
}

/// Flushes one kernel page on the receiving CPU, for a load experiment.
///
/// The real flush interrupt does page-table maintenance on the target:
/// it invalidates the translations the sender just changed. The bare
/// interrupt experiment showed that neither the machine-mode send nor the
/// interrupt itself troubles the board, so this isolates the part that
/// remains -- the `sfence.vma` the target executes.
fn stress_flush_callback() {
    use core::sync::atomic::AtomicU64;

    static DUMMY: AtomicU64 = AtomicU64::new(0);

    let addr = &DUMMY as *const AtomicU64 as usize;
    let page = addr & !(crate::mm::PAGE_SIZE - 1);
    crate::mm::tlb::TlbFlushOp::for_range(page..page + crate::mm::PAGE_SIZE).perform_on_current();
}

/// Sends a flush-performing interrupt to every other CPU, for a load
/// experiment.
///
/// Unlike [`send_bare_ipi_to_others`], the target runs the same kind of
/// `sfence.vma` a real flush interrupt does. Returns how many CPUs were
/// targeted.
pub fn send_flush_ipi_to_others() -> usize {
    const MAX_TARGETS: usize = 64;
    let Some(mapping) = hw_cpu_id_mapping() else {
        return 0;
    };
    let this_cpu = crate::cpu::CpuId::current_racy().as_usize();
    let mut targets = CpuSet::new_empty();
    let mut count = 0;
    for (cpu, _) in mapping.iter().enumerate() {
        if cpu == this_cpu || count >= MAX_TARGETS {
            continue;
        }
        targets.add(
            // SAFETY: the index is within the CPU range.
            unsafe { crate::cpu::CpuId::new_unchecked(cpu as u32) },
        );
        count += 1;
    }
    if count == 0 {
        return 0;
    }
    let _ = inter_processor_call(&targets, stress_flush_callback);
    count
}

/// Same as [`send_flush_ipi_to_others`], but waits for every target to finish.
///
/// The non-waiting version leaves the sender free to run ahead, so it never
/// exercises the collaboration the kernel actually performs: change the page
/// table, tell the others, and wait until every one of them has invalidated.
/// The wait needs local IRQs enabled, which they are in a kernel thread.
pub fn send_flush_ipi_to_others_waiting() -> usize {
    const MAX_TARGETS: usize = 64;
    let Some(mapping) = hw_cpu_id_mapping() else {
        return 0;
    };
    let this_cpu = crate::cpu::CpuId::current_racy().as_usize();
    let mut targets = CpuSet::new_empty();
    let mut count = 0;
    for (cpu, _) in mapping.iter().enumerate() {
        if cpu == this_cpu || count >= MAX_TARGETS {
            continue;
        }
        targets.add(
            // SAFETY: the index is within the CPU range.
            unsafe { crate::cpu::CpuId::new_unchecked(cpu as u32) },
        );
        count += 1;
    }
    if count == 0 {
        return 0;
    }
    let pending = inter_processor_call(&targets, stress_flush_callback);
    pending.wait();
    count
}

/// One full shootdown cycle, for a load experiment.
///
/// Maps a fresh kernel page, invalidates it on every CPU and drops the
/// mapping -- the same sequence a DMA mapping performs. The pure-sfence
/// experiment showed that invalidating a page that is not being modified
/// does not trouble the board, so this adds the one remaining ingredient:
/// the page table itself changes between the invalidations.
///
/// Returns how many CPUs the invalidation reached.
pub fn stress_shootdown_cycle() -> usize {
    use crate::{
        cpu::AtomicCpuSet,
        mm::{
            CachePolicy, FrameAllocOptions, PAGE_SIZE, PageFlags, PageProperty,
            PrivilegedPageFlags, kspace::kvirt_area::KVirtArea, tlb::TlbFlusher,
        },
    };

    let Ok(frame) = FrameAllocOptions::new().zeroed(false).alloc_frame() else {
        return 0;
    };
    let kva = KVirtArea::map_frames(
        PAGE_SIZE,
        0,
        core::iter::once(frame),
        PageProperty {
            flags: PageFlags::RW,
            cache: CachePolicy::Writeback,
            priv_flags: PrivilegedPageFlags::empty(),
        },
    );

    let target_cpus = AtomicCpuSet::new(CpuSet::new_full());
    let mut flusher = TlbFlusher::new(&target_cpus, crate::task::disable_preempt());
    let this_cpu = crate::cpu::CpuId::current_racy();
    let mut targets = 0;
    for cpu in crate::cpu::all_cpus() {
        if cpu != this_cpu {
            targets += 1;
        }
    }
    flusher.issue_tlb_flush(crate::mm::tlb::TlbFlushOp::for_range(kva.range()));
    flusher.dispatch_tlb_flush();
    flusher.sync_tlb_flush();
    // Dropping the area unmaps the page without flushing, exactly like the
    // virtual-area allocator the real path uses; the next cycle's flush covers
    // the reuse.
    drop(kva);

    targets
}

/// The range a touch cycle is currently exposing to the other CPUs.
static STRESS_TOUCH_VA: AtomicUsize = AtomicUsize::new(0);

/// Invalidates, touches and invalidates again, on the receiving CPU.
///
/// The first invalidation drops anything left over from the previous cycle so
/// the read cannot fault; the read then installs a valid translation, and the
/// second invalidation throws away one that is actually in use -- which is the
/// state a shared DMA buffer is in on a board whose interrupts move between
/// harts.
fn stress_touch_flush_callback() {
    let va = STRESS_TOUCH_VA.load(Ordering::Acquire);
    if va == 0 {
        return;
    }
    let page = va & !(crate::mm::PAGE_SIZE - 1);
    crate::mm::tlb::TlbFlushOp::for_range(page..page + crate::mm::PAGE_SIZE).perform_on_current();
    // SAFETY: the sender keeps the mapping installed until every target has
    // finished this callback, so the address is valid kernel memory here.
    unsafe { core::ptr::read_volatile(page as *const u64) };
    crate::mm::tlb::TlbFlushOp::for_range(page..page + crate::mm::PAGE_SIZE).perform_on_current();
}

/// One shootdown cycle in which every target holds a valid translation for
/// the page it is told to invalidate.
///
/// Returns how many CPUs were targeted.
pub fn stress_touch_flush_cycle() -> usize {
    use crate::mm::{
        CachePolicy, FrameAllocOptions, PAGE_SIZE, PageFlags, PageProperty, PrivilegedPageFlags,
        kspace::kvirt_area::KVirtArea,
    };

    let Ok(frame) = FrameAllocOptions::new().zeroed(false).alloc_frame() else {
        return 0;
    };
    let kva = KVirtArea::map_frames(
        PAGE_SIZE,
        0,
        core::iter::once(frame),
        PageProperty {
            flags: PageFlags::RW,
            cache: CachePolicy::Writeback,
            priv_flags: PrivilegedPageFlags::empty(),
        },
    );
    STRESS_TOUCH_VA.store(kva.range().start, Ordering::Release);

    let this_cpu = crate::cpu::CpuId::current_racy();
    let mut targets = CpuSet::new_empty();
    let mut count = 0;
    for cpu in crate::cpu::all_cpus() {
        if cpu != this_cpu {
            targets.add(cpu);
            count += 1;
        }
    }
    if count > 0 {
        let pending = inter_processor_call(&targets, stress_touch_flush_callback);
        pending.wait();
    }
    STRESS_TOUCH_VA.store(0, Ordering::Release);
    // Unmapped without flushing, like the virtual-area allocator; the next
    // cycle's invalidation covers the reuse.
    drop(kva);

    count
}

/// Pending remote inter-processor calls.
pub struct PendingIpis {
    cpus: CpuSet,
}

/// What each CPU was doing, for the one case that needs it.
///
/// When a sender waits a full second for a target and reports it, the question
/// is what the target was doing instead of taking the interrupt. Every marker
/// is one atomic store of "marker in the high bits, timebase in the low 48",
/// and only the stall report reads it back.
const MAX_ACT_CPUS: usize = 64;
const ACT_TICKS_MASK: u64 = 0x0000_FFFF_FFFF_FFFF;

static ACTIVITY: [AtomicU64; MAX_ACT_CPUS] = [const { AtomicU64::new(0) }; MAX_ACT_CPUS];

/// Marks the current CPU as busy with `marker` at the current time.
pub fn note_activity(marker: u32) {
    let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
    if cpu >= MAX_ACT_CPUS {
        return;
    }
    let ticks = crate::arch::read_tsc() & ACT_TICKS_MASK;
    ACTIVITY[cpu].store((u64::from(marker) << 48) | ticks, Ordering::Relaxed);
}

/// Returns the last marker of `cpu` and how long ago it was set, in
/// microseconds.
pub fn activity_of(cpu: usize) -> (&'static str, u64) {
    let value = ACTIVITY
        .get(cpu)
        .map_or(0, |cell| cell.load(Ordering::Relaxed));
    let marker = (value >> 48) as u32;
    let ticks = value & ACT_TICKS_MASK;
    let now = crate::arch::read_tsc() & ACT_TICKS_MASK;
    let age = now.saturating_sub(ticks).saturating_mul(1_000_000) / crate::arch::tsc_freq().max(1);
    (activity_name(marker), age)
}

fn activity_name(marker: u32) -> &'static str {
    match marker {
        ACT_PRINT => "print_wait",
        ACT_PRINT_WRITE => "print_write",
        ACT_SBI_SEND => "sbi_send",
        ACT_WAIT => "wait",
        ACT_IRQ => "irq",
        ACT_EARLY_PRINT => "early",
        ACT_SCHED_SPIN => "sched_spin",
        ACT_IPC => "ipc",
        ACT_ECALL_SEND => "ecall_send",
        ACT_ECALL_TIMER => "ecall_timer",
        ACT_BH_ENTER => "bh_enter",
        ACT_BH_HANDLER => "bh_handler",
        ACT_BH_AFTER_DP => "bh_after_dp",
        ACT_BH_AFTER_EN => "bh_after_en",
        ACT_SOFTIRQ => "softirq",
        ACT_TIMER_CB => "timer_cb",
        ACT_LOCK_SPIN => "lock_spin",
        ACT_RWLOCK_SPIN => "rw_spin",
        ACT_IRQ_ENTER => "irq_enter",
        ACT_PLIC_CLAIM => "plic_claim",
        _ => "idle",
    }
}

/// How many times each CPU entered the idle halt loop.
///
/// A monotonic counter that never needs a matching exit: if an idle CPU stops
/// counting, it stopped executing instructions (asleep in `wfi`); if it keeps
/// counting, it is running and the problem is elsewhere.
static HALT_COUNT: [AtomicU32; MAX_ACT_CPUS] = [const { AtomicU32::new(0) }; MAX_ACT_CPUS];

/// Records that the current CPU is about to halt.
pub fn note_halt() {
    let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
    if cpu < MAX_ACT_CPUS {
        HALT_COUNT[cpu].fetch_add(1, Ordering::Relaxed);
    }
}

/// Returns how many times `cpu` has halted since boot.
pub fn halt_count(cpu: usize) -> u32 {
    HALT_COUNT.get(cpu).map_or(0, |c| c.load(Ordering::Relaxed))
}

/// Time of the last trap taken on each CPU, for the stall report.
static LAST_TRAP: [AtomicU64; MAX_ACT_CPUS] = [const { AtomicU64::new(0) }; MAX_ACT_CPUS];

/// Traps taken per CPU, and interrupts among them that were the software
/// interrupt this kernel uses for IPIs.
///
/// A timer tick means every CPU takes at least one trap a second, so the age
/// alone cannot distinguish "alive" from "stopped for about a second". The
/// counters can: a target that keeps taking timer traps but takes no software
/// interrupt is alive and not receiving this interrupt.
static TRAP_COUNT: [AtomicU32; MAX_ACT_CPUS] = [const { AtomicU32::new(0) }; MAX_ACT_CPUS];
static IPI_TRAP_COUNT: [AtomicU32; MAX_ACT_CPUS] = [const { AtomicU32::new(0) }; MAX_ACT_CPUS];
static LAST_SEPC: [AtomicU64; MAX_ACT_CPUS] = [const { AtomicU64::new(0) }; MAX_ACT_CPUS];

/// Records a trap taken by the current CPU and where it interrupted.
pub fn note_trap(pc: usize) {
    let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
    if cpu >= MAX_ACT_CPUS {
        return;
    }
    LAST_TRAP[cpu].store(crate::arch::read_tsc() & ACT_TICKS_MASK, Ordering::Relaxed);
    LAST_SEPC[cpu].store(pc as u64, Ordering::Relaxed);
    TRAP_COUNT[cpu].fetch_add(1, Ordering::Relaxed);
}

/// Last exception/interrupt cause seen on each CPU, low bits only.
static LAST_SCAUSE: [AtomicU64; MAX_ACT_CPUS] = [const { AtomicU64::new(0) }; MAX_ACT_CPUS];
/// Interrupt-state snapshot taken when this CPU had a pending IPI of its own.
static IPI_SNAP: [AtomicU64; MAX_ACT_CPUS] = [const { AtomicU64::new(0) }; MAX_ACT_CPUS];
static IPI_SNAP_TICKS: [AtomicU64; MAX_ACT_CPUS] = [const { AtomicU64::new(0) }; MAX_ACT_CPUS];

/// Records the trap cause for the stall snapshot.
pub fn note_trap_cause(cause: u64) {
    let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
    if cpu < MAX_ACT_CPUS {
        // Keep the interrupt bit (63) and the cause code (low 8 bits); without
        // the former a timer interrupt and a load page fault both read as 5.
        let packed = (cause & 0xFF) | (((cause >> 63) & 1) << 8);
        LAST_SCAUSE[cpu].store(packed, Ordering::Relaxed);
    }
}

/// Whether this CPU has an inter-processor call it has not handled yet.
pub(crate) fn self_has_pending_ipi() -> bool {
    let cpu = crate::cpu::CpuId::current_racy();
    HAS_PENDING_IPIS.get_on_cpu(cpu).load(Ordering::Acquire)
}

/// Records the interrupt-controller state on this CPU, which is about to idle
/// while an inter-processor call is still pending for it.
///
/// The three ways this can happen need three different fixes: the firmware
/// never raised the interrupt (`sip.SSIP` clear), the interrupt is pending and
/// enabled but the core does not take it, or this kernel disabled it.
pub fn take_ipi_snapshot() {
    #[cfg(target_arch = "riscv64")]
    {
        let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
        if cpu >= MAX_ACT_CPUS {
            return;
        }
        let sip = riscv::register::sip::read().bits() as u64 & 0xFFFF;
        let sie = riscv::register::sie::read().bits() as u64 & 0xFFFF;
        let sie_enabled = riscv::register::sstatus::read().sie();
        let scause = LAST_SCAUSE[cpu].load(Ordering::Relaxed) & 0x1FF;
        let packed = sip | (sie << 16) | ((sie_enabled as u64) << 32) | (scause << 33);
        IPI_SNAP[cpu].store(packed, Ordering::Relaxed);
        IPI_SNAP_TICKS[cpu].store(crate::arch::read_tsc() & ACT_TICKS_MASK, Ordering::Relaxed);
    }
}

/// Returns `(sip, sie, sie_enabled, scause, age_us)` of the last snapshot on
/// `cpu`, or `None` if no snapshot was taken.
pub fn ipi_snapshot(cpu: usize) -> Option<(u16, u16, bool, u64, u64)> {
    let packed = IPI_SNAP.get(cpu)?.load(Ordering::Relaxed);
    let ticks = IPI_SNAP_TICKS.get(cpu)?.load(Ordering::Relaxed);
    if ticks == 0 {
        return None;
    }
    let now = crate::arch::read_tsc() & ACT_TICKS_MASK;
    let age_us =
        now.saturating_sub(ticks).saturating_mul(1_000_000) / crate::arch::tsc_freq().max(1);
    let scause_packed = (packed >> 33) & 0x1FF;
    let scause = ((scause_packed & 0xFF) | (((scause_packed >> 8) & 1) << 63)) as u64;
    Some((
        (packed & 0xFFFF) as u16,
        ((packed >> 16) & 0xFFFF) as u16,
        (packed >> 32) & 1 != 0,
        scause,
        age_us,
    ))
}

/// How many nested level-1 bottom-half invocations the current CPU is inside.
static BH_DEPTH: [AtomicU32; MAX_ACT_CPUS] = [const { AtomicU32::new(0) }; MAX_ACT_CPUS];

/// Enters one level of bottom-half processing.
pub fn bh_enter_depth() {
    let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
    if cpu < MAX_ACT_CPUS {
        BH_DEPTH[cpu].fetch_add(1, Ordering::Relaxed);
    }
}

/// Leaves one level of bottom-half processing.
pub fn bh_leave_depth() {
    let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
    if cpu < MAX_ACT_CPUS {
        BH_DEPTH[cpu].fetch_sub(1, Ordering::Relaxed);
    }
}

/// Returns how many nested bottom-half invocations `cpu` is inside.
pub fn bh_depth(cpu: usize) -> u32 {
    BH_DEPTH.get(cpu).map_or(0, |c| c.load(Ordering::Relaxed))
}

/// Records that the current CPU is handling a software (IPI) interrupt.
pub fn note_ipi_trap() {
    let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
    if cpu < MAX_ACT_CPUS {
        IPI_TRAP_COUNT[cpu].fetch_add(1, Ordering::Relaxed);
    }
}

/// Returns `(traps, ipi_traps, last_sepc)` for `cpu`.
pub fn trap_stats(cpu: usize) -> (u32, u32, u64) {
    (
        TRAP_COUNT.get(cpu).map_or(0, |c| c.load(Ordering::Relaxed)),
        IPI_TRAP_COUNT
            .get(cpu)
            .map_or(0, |c| c.load(Ordering::Relaxed)),
        LAST_SEPC.get(cpu).map_or(0, |c| c.load(Ordering::Relaxed)),
    )
}

/// How long ago `cpu` last took a trap, in microseconds.
///
/// A small age means the CPU is alive and taking interrupts, so an interrupt
/// that did not arrive is a delivery problem rather than a stalled CPU.
pub fn trap_age_us(cpu: usize) -> u64 {
    let ticks = LAST_TRAP
        .get(cpu)
        .map_or(0, |cell| cell.load(Ordering::Relaxed));
    let now = crate::arch::read_tsc() & ACT_TICKS_MASK;
    now.saturating_sub(ticks).saturating_mul(1_000_000) / crate::arch::tsc_freq().max(1)
}

/// When each CPU last entered the firmware call that arms its timer.
///
/// The same fact a CLINT `mtimecmp` read gives from outside, obtained from
/// inside and therefore readable even when the platform window cannot be
/// mapped. It answers exactly one question: is this CPU still running any
/// timer path (the periodic tick or a one-shot). It does not distinguish a
/// long IRQ-off section from a firmware call that never returned -- both
/// stop the timestamp -- so it must not be used as machine-mode evidence.
static LAST_SET_TIMER: [AtomicU64; MAX_ACT_CPUS] = [const { AtomicU64::new(0) }; MAX_ACT_CPUS];

/// Records that the current CPU is arming its timer.
pub fn note_set_timer() {
    let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
    if cpu < MAX_ACT_CPUS {
        LAST_SET_TIMER[cpu].store(crate::arch::read_tsc() & ACT_TICKS_MASK, Ordering::Relaxed);
    }
}

/// How long ago `cpu` last armed its timer, in microseconds.
pub fn set_timer_age_us(cpu: usize) -> u64 {
    let ticks = LAST_SET_TIMER
        .get(cpu)
        .map_or(0, |cell| cell.load(Ordering::Relaxed));
    let now = crate::arch::read_tsc() & ACT_TICKS_MASK;
    now.saturating_sub(ticks).saturating_mul(1_000_000) / crate::arch::tsc_freq().max(1)
}

/// Printing on the console (the suspected source of long IRQ-off windows).
///
/// `ACT_PRINT` means the CPU is in the console path but has not acquired the
/// device lock yet -- it is queued, with IRQs already disabled.
pub const ACT_PRINT: u32 = 1;
/// Holding the console lock and writing to the devices.
pub const ACT_PRINT_WRITE: u32 = 5;
/// Entering the firmware call that sends an interrupt.
pub const ACT_SBI_SEND: u32 = 2;
/// Spinning in [`PendingIpis::wait`].
pub const ACT_WAIT: u32 = 3;
/// Handling an interrupt.
pub const ACT_IRQ: u32 = 4;
/// Spinning for another CPU to finish a task handoff (`before_switching_to`).
pub const ACT_SCHED_SPIN: u32 = 7;
/// Running the inter-processor call path on the receiving side.
pub const ACT_IPC: u32 = 8;
/// Inside the firmware call that sends an interrupt.
pub const ACT_ECALL_SEND: u32 = 9;
/// Inside the firmware call that arms the timer.
pub const ACT_ECALL_TIMER: u32 = 10;

/// Inside the level-1 bottom half (`bottom_half::process`).
pub const ACT_BH_ENTER: u32 = 11;
/// Just after `note_activity(ACT_BH_ENTER)` and before `disable_preempt`.
pub const ACT_BH_AFTER_DP: u32 = 15;
/// Just after local IRQs were re-enabled in the bottom half.
pub const ACT_BH_AFTER_EN: u32 = 16;
/// About to run the bottom-half handler with local IRQs disabled.
pub const ACT_BH_HANDLER: u32 = 12;
/// Inside a softirq callback.
pub const ACT_SOFTIRQ: u32 = 13;
/// Running the per-tick callbacks inside the timer interrupt.
pub const ACT_TIMER_CB: u32 = 14;

/// Writing to the firmware console one byte at a time (`early_println`).
///
/// Each byte is a machine-mode call, and the firmware waits for the UART
/// inside it, so a hart here can be out of touch for a long time.
pub const ACT_EARLY_PRINT: u32 = 6;

/// Spinning to acquire a spin lock, slow path only.
///
/// Unlike every other marker this one is written while the CPU is in a
/// critical section that has no other breadcrumb: a hart that lost its
/// interrupts while holding a lock, or while waiting for one, shows exactly
/// the same "no traps of any kind" picture as a hart parked in machine mode.
/// The site recorded with it names the `.lock()` call that is stuck.
pub const ACT_LOCK_SPIN: u32 = 17;

/// Spinning to acquire an `RwLock`, slow path only.
///
/// `RwLock` has its own atomic loop and does not go through `SpinLock`, so
/// without this a hart parked in `top_half::process` on a callback-table
/// read lock looks exactly like a hart that stopped executing.
pub const ACT_RWLOCK_SPIN: u32 = 18;

/// Entering `handle_irq`, with the kind recorded for the stall report.
pub const ACT_IRQ_ENTER: u32 = 19;

/// Inside the external-interrupt claim loop, i.e. about to read the PLIC.
///
/// A hart that stops taking traps and shows this marker was inside the
/// claim/dispatch loop when it froze -- either the PLIC keeps returning a
/// line whose callbacks never run, or the claim's MMIO read itself never
/// returned. Both are invisible to every other marker.
pub const ACT_PLIC_CLAIM: u32 = 20;

/// The last external interrupt number each CPU claimed, and how many it has
/// claimed since boot.
static LAST_EXT_IRQ: [AtomicU32; MAX_ACT_CPUS] = [const { AtomicU32::new(0) }; MAX_ACT_CPUS];
static EXT_IRQ_COUNT: [AtomicU32; MAX_ACT_CPUS] = [const { AtomicU32::new(0) }; MAX_ACT_CPUS];

/// Records that the current CPU claimed external interrupt `irq_num`.
pub fn note_ext_irq(irq_num: u8) {
    let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
    if cpu < MAX_ACT_CPUS {
        LAST_EXT_IRQ[cpu].store(u32::from(irq_num), Ordering::Relaxed);
        EXT_IRQ_COUNT[cpu].fetch_add(1, Ordering::Relaxed);
    }
}

/// Returns the last claimed external interrupt number, or 0 for none.
pub fn last_ext_irq(cpu: usize) -> u32 {
    LAST_EXT_IRQ
        .get(cpu)
        .map_or(0, |c| c.load(Ordering::Relaxed))
}

/// Returns how many external interrupts `cpu` has claimed since boot.
pub fn ext_irq_count(cpu: usize) -> u32 {
    EXT_IRQ_COUNT
        .get(cpu)
        .map_or(0, |c| c.load(Ordering::Relaxed))
}

/// External interrupts masked because their source could not be quiesced.
static IRQ_STORMS: AtomicU32 = AtomicU32::new(0);
static LAST_STORM_IRQ: AtomicU32 = AtomicU32::new(u32::MAX);

/// Records that `irq_num` was masked after a storm.
pub fn note_irq_storm(irq_num: u8) {
    IRQ_STORMS.fetch_add(1, Ordering::Relaxed);
    LAST_STORM_IRQ.store(u32::from(irq_num), Ordering::Relaxed);
}

/// Returns how many sources have been masked after a storm.
pub fn irq_storm_count() -> u32 {
    IRQ_STORMS.load(Ordering::Relaxed)
}

/// Returns the last source masked after a storm, or `u32::MAX` for none.
pub fn last_storm_irq() -> u32 {
    LAST_STORM_IRQ.load(Ordering::Relaxed)
}

/// The kind of the last interrupt each CPU started handling.
static LAST_IRQ_KIND: [AtomicU32; MAX_ACT_CPUS] = [const { AtomicU32::new(0) }; MAX_ACT_CPUS];

/// Records that the current CPU entered `handle_irq` for `kind`.
pub fn note_irq_enter(kind: u32) {
    let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
    if cpu < MAX_ACT_CPUS {
        LAST_IRQ_KIND[cpu].store(kind, Ordering::Relaxed);
        note_activity(ACT_IRQ_ENTER);
    }
}

/// Returns `"timer"`, `"external"`, `"software"`, or `"none"` for `cpu`.
pub fn last_irq_kind(cpu: usize) -> &'static str {
    match LAST_IRQ_KIND
        .get(cpu)
        .map_or(0, |cell| cell.load(Ordering::Relaxed))
    {
        1 => "timer",
        2 => "external",
        3 => "software",
        _ => "none",
    }
}

/// When each CPU last entered the inter-processor call path.
///
/// The same self-contained kind of measurement as `set_timer`: no firmware,
/// no MMIO, no serial. It answers whether the target was inside an IPI when
/// it stopped (age of a few microseconds), or had not taken one for a long
/// time (age near the whole window).
static LAST_IPI: [AtomicU64; MAX_ACT_CPUS] = [const { AtomicU64::new(0) }; MAX_ACT_CPUS];

/// Records that the current CPU entered the inter-processor call path.
pub fn note_last_ipi() {
    let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
    if cpu < MAX_ACT_CPUS {
        LAST_IPI[cpu].store(crate::arch::read_tsc() & ACT_TICKS_MASK, Ordering::Relaxed);
    }
}

/// How long ago `cpu` entered the inter-processor call path, in microseconds.
pub fn last_ipi_age_us(cpu: usize) -> u64 {
    let ticks = LAST_IPI
        .get(cpu)
        .map_or(0, |cell| cell.load(Ordering::Relaxed));
    let now = crate::arch::read_tsc() & ACT_TICKS_MASK;
    now.saturating_sub(ticks).saturating_mul(1_000_000) / crate::arch::tsc_freq().max(1)
}

/// Where each CPU is spinning for a lock, or 0 when it is not spinning.
static LOCK_SPIN_SITE: [AtomicUsize; MAX_ACT_CPUS] = [const { AtomicUsize::new(0) }; MAX_ACT_CPUS];

/// The lock each spinning CPU is waiting for, or 0 when it is not spinning.
///
/// The call site cannot tell two instances created by the same statement
/// apart; the address can. Two CPUs spinning on the same address are waiting
/// for each other (or for one third party), on different addresses they are
/// not.
static LOCK_SPIN_ADDR: [AtomicUsize; MAX_ACT_CPUS] = [const { AtomicUsize::new(0) }; MAX_ACT_CPUS];

/// Records that the current CPU is spinning to acquire the lock at `addr`.
///
/// Called only from the lock's slow path, after the fast `compare_exchange`
/// failed, so an uncontended lock pays nothing. The marker is written once
/// rather than per iteration: the time from the first failed attempt is what
/// the age should measure.
#[track_caller]
pub fn note_lock_spin(addr: usize) {
    let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
    if cpu < MAX_ACT_CPUS {
        let site = core::panic::Location::caller() as *const _ as usize;
        LOCK_SPIN_ADDR[cpu].store(addr, Ordering::Relaxed);
        LOCK_SPIN_SITE[cpu].store(site, Ordering::Relaxed);
        note_activity(ACT_LOCK_SPIN);
    }
}

/// Records that the current CPU is spinning for an `RwLock` at `addr`.
///
/// The same bookkeeping as [`note_lock_spin`], with a different marker so a
/// report can tell the two lock kinds apart; `RwLock` does not go through
/// `SpinLock` and would otherwise be invisible.
#[track_caller]
pub fn note_rw_spin(addr: usize) {
    let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
    if cpu < MAX_ACT_CPUS {
        let site = core::panic::Location::caller() as *const _ as usize;
        LOCK_SPIN_ADDR[cpu].store(addr, Ordering::Relaxed);
        LOCK_SPIN_SITE[cpu].store(site, Ordering::Relaxed);
        note_activity(ACT_RWLOCK_SPIN);
    }
}

/// Records that the current CPU's spin ended because it acquired the lock.
pub fn note_lock_spin_done() {
    let cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
    if cpu < MAX_ACT_CPUS {
        LOCK_SPIN_ADDR[cpu].store(0, Ordering::Relaxed);
        if LOCK_SPIN_SITE[cpu].swap(0, Ordering::Relaxed) != 0 {
            // Only clear the activity if it is still a lock marker. An
            // interrupt handler that ran while we spun may have written a
            // fresher one, and that one is the truth about what we are doing
            // now.
            let value = ACTIVITY[cpu].load(Ordering::Relaxed);
            let marker = (value >> 48) as u32;
            if marker == ACT_LOCK_SPIN || marker == ACT_RWLOCK_SPIN {
                ACTIVITY[cpu].store(0, Ordering::Relaxed);
            }
        }
    }
}

/// Returns the source location of `cpu`'s current lock spin, if any.
pub fn lock_spin_site(cpu: usize) -> Option<(&'static str, u32)> {
    let site = LOCK_SPIN_SITE.get(cpu)?.load(Ordering::Relaxed);
    if site == 0 {
        return None;
    }
    // SAFETY: the only values ever stored are `&'static Location` addresses
    // recorded by `note_lock_spin` in this module, and zero.
    let location = unsafe { &*(site as *const core::panic::Location<'static>) };
    Some((location.file(), location.line()))
}

/// Returns the address of the lock `cpu` is spinning for, if any.
pub fn lock_spin_addr(cpu: usize) -> Option<usize> {
    let addr = LOCK_SPIN_ADDR.get(cpu)?.load(Ordering::Relaxed);
    (addr != 0).then_some(addr)
}

/// Describes `cpu`'s machine-level timer and interrupt registers.
///
/// The only view that does not require the target to keep executing: `MSIP`
/// says whether an interrupt was raised and not taken, and a positive
/// `cmp_lag_ms` says the target's timer deadline is long past and was never
/// handled. `none` means the board's CLINT was not mapped (see
/// `ASTERINAS_TIMER_MMIO` at boot).
fn describe_clint(cpu: usize) -> alloc::string::String {
    #[cfg(target_arch = "riscv64")]
    {
        // The platform indexes `MSIP` and `mtimecmp` by *hardware* hart id,
        // which is not the logical CPU id and is not stable across boots
        // (the board may come up on any hart). Reading by logical id would
        // silently describe a different hart -- and look perfectly normal.
        let Some(hwid) = hw_cpu_id_mapping()
            .and_then(|ids| ids.get(cpu))
            .map(|id| id.hw_id())
        else {
            return alloc::string::String::from("clint-nohwid");
        };
        // "Not readable" and "read as zero" must not look the same: a
        // disabled read means the diagnostic is switched off on purpose, an
        // unmapped window means the observation is blocked (which is itself
        // a finding about the firmware), and a failed second sample means
        // the window is there and the read path misbehaved.
        //
        // The switch is checked first, and it matters most here: on this
        // board a read raises a fatal load fault, so reading from the stall
        // path would panic while the report is being built and destroy the
        // evidence instead of collecting it.
        if !crate::arch::clint::is_read_enabled() {
            return alloc::string::String::from("clint-off");
        }
        if !crate::arch::clint::is_mapped() {
            return alloc::string::String::from("clint-unmapped");
        }
        // Two samples a millisecond apart, not one: a single `mtimecmp`
        // value cannot separate "this hart stopped re-arming its timer" from
        // "the timer is fine". `cmp_delta_us` follows real time (~+1000)
        // when the deadline was never moved, and stays near zero or goes
        // negative when the hart re-armed it during the window. The wait is
        // a busy read of `mtime`: sleeping on a timer here would run the
        // machinery under study.
        crate::arch::clint::hart_snapshot(hwid, 1000).map_or(
            alloc::string::String::from("clint-readfail"),
            |state| {
                // `idx` is the array index actually used. It equals `hwid`
                // when the device tree lists the harts from 0 upward, and
                // differs exactly when it does not -- the case where reading
                // by hart id would silently describe another core.
                let index = if state.msip_index == state.mtimecmp_index {
                    alloc::format!("{}", state.msip_index)
                } else {
                    alloc::format!("{}/{}", state.msip_index, state.mtimecmp_index)
                };
                alloc::format!(
                    "hwid={} idx={} msip={}/{} cmp_lag_ms={} cmp_delta_us={}",
                    hwid,
                    index,
                    state.msip & 1,
                    state.msip_after & 1,
                    state.lag_us / 1000,
                    state.lag_delta_us
                )
            },
        )
    }
    #[cfg(not(target_arch = "riscv64"))]
    {
        let _ = cpu;
        alloc::string::String::from("none")
    }
}

/// How long a sender waits before it says it is stuck.
const STALL_REPORT_MS: u64 = 1000;

/// Time spent inside [`PendingIpis::wait`], accumulated for diagnosis.
///
/// A sender waiting for its targets is the one part of the real flush
/// collaboration a load experiment can otherwise miss. How long the wait
/// takes separates a queue (everybody waits a little, growing with load)
/// from a target that does not answer (a few waits that never end).
/// Whether the stall path resends an interrupt that never arrived.
///
/// The resend is a diagnostic stuck inside the failure: if the board dies
/// within a tenth of a second of the stall report, this is one of the few
/// things that happened in between, so it needs its own switch to be ruled in
/// or out.
static RESEND_ENABLED: AtomicBool = AtomicBool::new(true);

/// Enables or disables the forced resend in the stall path.
pub fn set_ipi_resend(enabled: bool) {
    RESEND_ENABLED.store(enabled, Ordering::Relaxed);
}

static WAIT_CALLS: AtomicU32 = AtomicU32::new(0);
static WAIT_TICKS: AtomicU64 = AtomicU64::new(0);
static WAIT_MAX_TICKS: AtomicU64 = AtomicU64::new(0);

/// Returns `(calls, total_ticks, max_ticks)` since the previous call and
/// resets the totals.
pub fn take_ipi_wait_stats() -> (u64, u64, u64) {
    (
        u64::from(WAIT_CALLS.swap(0, Ordering::Relaxed)),
        WAIT_TICKS.swap(0, Ordering::Relaxed),
        WAIT_MAX_TICKS.swap(0, Ordering::Relaxed),
    )
}

/// The CPUs that still have an unreported pending inter-processor call.
fn pending_ipi_mask() -> u64 {
    let mut mask = 0u64;
    for cpu_id in crate::cpu::all_cpus() {
        if HAS_PENDING_IPIS.get_on_cpu(cpu_id).load(Ordering::Acquire) {
            mask |= 1 << u32::from(cpu_id);
        }
    }
    mask
}

impl PendingIpis {
    pub(crate) fn new_empty() -> Self {
        Self {
            cpus: CpuSet::new_empty(),
        }
    }

    fn add(&mut self, cpu_id: crate::cpu::CpuId) {
        self.cpus.add(cpu_id);
    }

    pub(crate) fn extend(&mut self, other: &Self) {
        for cpu_id in other.cpus.iter() {
            self.add(cpu_id);
        }
    }

    /// Waits until all pending remote processors have handled their IPIs.
    ///
    /// # Panics
    ///
    /// This method panics if local IRQs are disabled. Waiting for remote IPIs
    /// with local IRQs disabled can deadlock if one of the remote processors is
    /// also waiting for this processor to handle an IPI.
    pub fn wait(&self) {
        assert!(
            crate::arch::irq::is_local_enabled(),
            "waiting for remote inter-processor calls with IRQs disabled"
        );

        IPI_WAITS.fetch_add(1, Ordering::Relaxed);
        note_activity(ACT_WAIT);
        let call_started = crate::arch::read_tsc();
        for cpu_id in self.cpus.iter() {
            // Counters at the moment this target's wait begins: no allocation,
            // one read each of two per-CPU counters.
            let (base_traps, base_ipis, _) = trap_stats(cpu_id.as_usize());
            let base_halt = halt_count(cpu_id.as_usize());
            // Wait until there are no pending IPIs on the target CPU.
            //
            // Note that if new IPIs arrive to that CPU in the meantime, we
            // will also wait for them. This is fine because there usually
            // aren't too many IPIs in common cases.
            let started = crate::arch::read_tsc();
            let frequency = crate::arch::tsc_freq().max(1);
            let mut reported = false;
            // A second look one second after the first report. In the
            // watchdog-shaped failure the machine stays alive for another
            // ~30 s, and this says whether the target ever ran again.
            let mut followup_at: Option<u64> = None;
            while HAS_PENDING_IPIS.get_on_cpu(cpu_id).load(Ordering::Acquire) {
                // A board that stops dead with nothing in the log and resets
                // through its hardware watchdog looks exactly like this loop
                // never finishing: the sender waits for a target that never
                // takes the interrupt. Say so, once, before the watchdog
                // erases the evidence.
                if !reported {
                    let elapsed = crate::arch::read_tsc().saturating_sub(started);
                    let frequency = crate::arch::tsc_freq().max(1);
                    if elapsed.saturating_mul(1_000) >= frequency.saturating_mul(STALL_REPORT_MS) {
                        let (target_activity, target_age_us) = activity_of(cpu_id.as_usize());
                        let target_trap_age_us = trap_age_us(cpu_id.as_usize());
                        let target_bh_depth = bh_depth(cpu_id.as_usize());
                        let (_, _, target_sepc) = trap_stats(cpu_id.as_usize());
                        // What the target's interrupt state said the last
                        // time it was about to idle with this CPU's IPI
                        // still pending: the one fact that separates "the
                        // firmware never raised it" from "raised but not
                        // taken" from "this kernel disabled it".
                        let target_irq_state = ipi_snapshot(cpu_id.as_usize()).map_or(
                            alloc::string::String::from("none"),
                            |(sip, sie, sie_on, scause, age)| {
                                alloc::format!(
                                    "sip={:#x} sie={:#x} on={} scause={:#x} age_us={}",
                                    sip,
                                    sie,
                                    u8::from(sie_on),
                                    scause,
                                    age
                                )
                            },
                        );
                        // Is the target inside an IRQ-off window right now?
                        // A window that never ends never reaches the
                        // "longest completed" statistic, and this is the
                        // form the failure takes.
                        let target_irqoff = crate::irq::current_irq_off_us(cpu_id.as_usize())
                            .map_or(alloc::string::String::from("none"), |us| {
                                let src = crate::irq::current_irq_off_site(cpu_id.as_usize())
                                    .map_or(alloc::string::String::from("?"), |(file, line)| {
                                        let short = file.rsplit('/').next().unwrap_or(file);
                                        alloc::format!("{}@{}", short, line)
                                    });
                                alloc::format!("{} {}", us, src)
                            });
                        // Is the target spinning for a lock right now? This
                        // names the `.lock()` call, which is the one critical
                        // section that other markers cannot show: a hart
                        // parked in a lock has no traps (IRQs off in the trap
                        // handler or a `LocalIrqDisabled` guard) and no other
                        // breadcrumb at all.
                        let target_lock = lock_spin_site(cpu_id.as_usize()).map_or(
                            alloc::string::String::from("none"),
                            |(file, line)| {
                                let short = file.rsplit('/').next().unwrap_or(file);
                                alloc::format!("{}:{}", short, line)
                            },
                        );
                        // The lock's address, which tells two CPUs spinning on
                        // the same instance (a mutual wait) from two CPUs that
                        // merely happen to wait for locks of the same type.
                        let target_lock_addr = lock_spin_addr(cpu_id.as_usize())
                            .map_or(alloc::string::String::from("none"), |addr| {
                                alloc::format!("{:#x}", addr)
                            });
                        // The machine-level view: registers another hart can
                        // read whether or not the target is executing. `MSIP`
                        // set means an interrupt was raised and is not being
                        // taken; a positive `cmp_lag_ms` means the target's
                        // timer deadline is in the past and was not handled.
                        let target_clint = describe_clint(cpu_id.as_usize());
                        // Every CPU's state at the stall: if several are queued
                        // for the console lock at once, the failure is a
                        // system-wide stall, not one hart misbehaving.
                        let mut all_activity = alloc::string::String::new();
                        for cpu in crate::cpu::all_cpus() {
                            let (name, age) = activity_of(cpu.as_usize());
                            if !all_activity.is_empty() {
                                all_activity.push(' ');
                            }
                            all_activity.push_str(&alloc::format!(
                                "{}:{}:{}us",
                                u32::from(cpu),
                                name,
                                age
                            ));
                        }
                        let (target_traps, target_ipi_traps, _) = trap_stats(cpu_id.as_usize());
                        let traps_since = target_traps.wrapping_sub(base_traps);
                        let ipis_since = target_ipi_traps.wrapping_sub(base_ipis);
                        // A healthy idle CPU keeps entering its idle loop; a
                        // stopped one does not. Without this, `activity=idle`
                        // looks the same in both cases.
                        let halts_since = halt_count(cpu_id.as_usize()).wrapping_sub(base_halt);
                        // Positive control: the waiter is inside this function, so its
                        // own marker must read `wait`. If it does not, the markers are not
                        // wired up and the target reading means nothing.
                        let waiter_cpu = u32::from(crate::cpu::CpuId::current_racy()) as usize;
                        let (waiter_activity, waiter_age_us) = activity_of(waiter_cpu);
                        crate::early_println!(
                            "ASTERINAS_IPI_WAIT_STALLED waiter_cpu={} target_cpu={} waited_us={} \
pending_mask={:#x} waiter_activity={} waiter_activity_age_us={} target_activity={} target_activity_age_us={} target_trap_age_us={} target_sepc={:#x} target_irqoff_us={} target_irq_state={} target_wait_ms={} target_traps_since_wait={} target_bh_depth={} target_lock={} target_lock_addr={} target_clint={} target_settimer_age_us={} target_irq_kind={} target_last_ipi_age_us={} target_last_ext_irq={} target_ext_irqs={} target_ipi_traps_since_wait={} target_halts_since_wait={} all_activity={}",
                            u32::from(crate::cpu::CpuId::current_racy()),
                            u32::from(cpu_id),
                            elapsed.saturating_mul(1_000_000) / frequency,
                            pending_ipi_mask(),
                            waiter_activity,
                            waiter_age_us,
                            target_activity,
                            target_age_us,
                            target_trap_age_us,
                            target_sepc,
                            target_irqoff,
                            target_irq_state,
                            elapsed.saturating_mul(1_000) / frequency,
                            traps_since,
                            target_bh_depth,
                            target_lock,
                            target_lock_addr,
                            target_clint,
                            set_timer_age_us(cpu_id.as_usize()),
                            last_irq_kind(cpu_id.as_usize()),
                            last_ipi_age_us(cpu_id.as_usize()),
                            last_ext_irq(cpu_id.as_usize()),
                            ext_irq_count(cpu_id.as_usize()),
                            ipis_since,
                            halts_since,
                            all_activity
                        );
                        // Coalescing makes a lost interrupt permanent: the
                        // pending flag stays set, so every later send is folded
                        // into an interrupt that will never arrive. Resend once,
                        // unconditionally, so a lost interrupt heals instead of
                        // parking this CPU until the watchdog fires.
                        if RESEND_ENABLED.load(Ordering::Relaxed)
                            && let Some(&hw_cpu_id) =
                                hw_cpu_id_mapping().and_then(|ids| ids.get(cpu_id.as_usize()))
                        {
                            let guard = crate::task::disable_preempt();
                            IPI_RESENT.fetch_add(1, Ordering::Relaxed);
                            let _ = crate::arch::irq::send_ipi(hw_cpu_id, &guard as _);
                        }
                        reported = true;
                        followup_at = Some(crate::arch::read_tsc().saturating_add(frequency));
                    } else if let Some(at) = followup_at
                        && crate::arch::read_tsc() >= at
                    {
                        let (activity, activity_age) = activity_of(cpu_id.as_usize());
                        crate::early_println!(
                            "ASTERINAS_IPI_WAIT_STILL target_cpu={} pending_mask={:#x} \
activity={} activity_age_us={} halts_since_wait={} trap_age_us={}",
                            u32::from(cpu_id),
                            pending_ipi_mask(),
                            activity,
                            activity_age,
                            halt_count(cpu_id.as_usize()).wrapping_sub(base_halt),
                            trap_age_us(cpu_id.as_usize())
                        );
                        followup_at = None;
                    }
                }
                core::hint::spin_loop();
            }
        }
        note_activity(0);
        let waited = crate::arch::read_tsc().saturating_sub(call_started);
        WAIT_CALLS.fetch_add(1, Ordering::Relaxed);
        WAIT_TICKS.fetch_add(waited, Ordering::Relaxed);
        WAIT_MAX_TICKS.fetch_max(waited, Ordering::Relaxed);
    }
}

/// A sender that carries necessary information to send inter-processor interrupts.
///
/// The purpose of exporting this type is to enable the users to check whether
/// [`IPI_SENDER`] has been initialized.
pub(crate) struct IpiSender {
    hw_cpu_ids: Box<[HwCpuId]>,
}

/// Inter-processor calls sent since boot (diagnostics).
/// Which callback is behind the inter-processor traffic (diagnostics).
pub const IPI_KIND_NAMES: [&str; 4] = ["flush", "preempt", "memb", "other"];

static SENT_KIND: [AtomicU32; 4] = [
    AtomicU32::new(0),
    AtomicU32::new(0),
    AtomicU32::new(0),
    AtomicU32::new(0),
];
static RECEIVED_KIND: [AtomicU32; 4] = [
    AtomicU32::new(0),
    AtomicU32::new(0),
    AtomicU32::new(0),
    AtomicU32::new(0),
];

fn kind_of(call_fn: fn()) -> usize {
    let address = call_fn as usize;
    if address == crate::mm::tlb::do_remote_flush as usize {
        0
    } else if address == crate::task::scheduler::set_need_preempt_remote as usize {
        1
    } else if address == do_synchronous_memory_barrier as usize {
        2
    } else {
        3
    }
}

/// Returns (sent, received) per kind, in the order of [`IPI_KIND_NAMES`].
pub fn ipi_kind_counters() -> ([u32; 4], [u32; 4]) {
    let mut sent = [0; 4];
    let mut received = [0; 4];
    for index in 0..4 {
        sent[index] = SENT_KIND[index].load(Ordering::Relaxed);
        received[index] = RECEIVED_KIND[index].load(Ordering::Relaxed);
    }
    (sent, received)
}

static IPI_SENT: AtomicU32 = AtomicU32::new(0);
/// Completed IPI waits since boot (diagnostics).
static IPI_WAITS: AtomicU32 = AtomicU32::new(0);
/// Sends skipped because the target already had an interrupt pending.
static IPI_COALESCED: AtomicU32 = AtomicU32::new(0);
/// Interrupts resent because a wait had stalled for a second.
static IPI_RESENT: AtomicU32 = AtomicU32::new(0);

/// Returns how many sends were folded into an already-pending interrupt.
pub fn coalesced_ipi_count() -> u32 {
    IPI_COALESCED.load(Ordering::Relaxed)
}

/// Returns how many stalled waits forced an interrupt to be resent.
pub fn resent_ipi_count() -> u32 {
    IPI_RESENT.load(Ordering::Relaxed)
}

/// Returns how many firmware calls have been made to send an interrupt.
pub fn sbi_ipi_call_count() -> u32 {
    #[cfg(target_arch = "riscv64")]
    {
        crate::arch::irq::sbi_ipi_calls()
    }
    #[cfg(not(target_arch = "riscv64"))]
    {
        0
    }
}

/// Measures what a measurement costs, and what entering firmware costs.
///
/// Returns `(timebase_read_ticks, version_call_ticks, ipi_call_ticks)` in
/// timebase ticks, averaged where it makes sense. Three explanations for a
/// slow send call are otherwise conflated:
///
/// - if reading the timebase is itself expensive, every measurement made with
///   it is inflated by that amount;
/// - if a firmware call with no side effects (`get_spec_version`) is just as
///   expensive, the cost is the machine-mode entry, not the send;
/// - only if both are cheap and the send is not is the send path itself the
///   problem.
pub fn probe_sbi_costs() -> (u64, u64, u64) {
    #[cfg(not(target_arch = "riscv64"))]
    {
        return (0, 0, 0);
    }
    #[cfg(target_arch = "riscv64")]
    {
        const ROUNDS: u64 = 32;

        let mut read_total = 0u64;
        for _ in 0..ROUNDS {
            let start = crate::arch::read_tsc();
            let _ = crate::arch::read_tsc();
            read_total += crate::arch::read_tsc().wrapping_sub(start);
        }
        let read_ticks = read_total / ROUNDS;

        let mut version_total = 0u64;
        for _ in 0..ROUNDS {
            let start = crate::arch::read_tsc();
            let _ = sbi_rt::get_spec_version();
            version_total += crate::arch::read_tsc().wrapping_sub(start);
        }
        // Subtract the two timebase reads that bracket each call.
        let version_ticks = (version_total / ROUNDS).saturating_sub(read_ticks);

        let mut ipi_total = 0u64;
        for _ in 0..ROUNDS {
            let start = crate::arch::read_tsc();
            let _ = send_bare_ipi_to_others(false);
            ipi_total += crate::arch::read_tsc().wrapping_sub(start);
        }
        let ipi_ticks = (ipi_total / ROUNDS).saturating_sub(read_ticks);

        (read_ticks, version_ticks, ipi_ticks)
    }
}

/// Returns `(calls, total_ticks, max_ticks, max_cpu, max_uptime_ms)` for the
/// firmware send calls made so far and resets the maximum (the call total is
/// cumulative; the ticks and the maximum cover the previous call).
///
/// The caller converts ticks to time with [`crate::arch::tsc_freq`].
pub fn sbi_ipi_call_stats() -> (u64, u64, u64, u32, u64) {
    #[cfg(target_arch = "riscv64")]
    {
        crate::arch::irq::take_sbi_ipi_call_stats()
    }
    #[cfg(not(target_arch = "riscv64"))]
    {
        (0, 0, 0, 0, 0)
    }
}

/// Serializes the firmware calls that send interrupts across harts.
pub fn set_sbi_ipi_serialized(enabled: bool) {
    #[cfg(target_arch = "riscv64")]
    {
        crate::arch::irq::set_sbi_ipi_serialized(enabled);
    }
    #[cfg(not(target_arch = "riscv64"))]
    {
        let _ = enabled;
    }
}

/// Returns `(received on this CPU, sent since boot, waits since boot)`.
///
/// Diagnostics for a CPU that stops doing both: if the receive count stops
/// while the send count keeps growing, the stuck CPU is the target of the
/// calls; the reverse means it is the sender.
pub fn ipi_counters() -> (u32, u32, u32) {
    let received = IPI_RECEIVED
        .get_on_cpu(crate::cpu::CpuId::current_racy())
        .load(Ordering::Relaxed);
    (
        received,
        IPI_SENT.load(Ordering::Relaxed),
        IPI_WAITS.load(Ordering::Relaxed),
    )
}

/// The [`IpiSender`] singleton.
pub(crate) static IPI_SENDER: Once<IpiSender> = Once::new();
/// Returns the hardware CPU ID mapping used for inter-processor communication.
///
/// `None` before SMP initialization has installed the mapping.
pub(crate) fn hw_cpu_id_mapping() -> Option<&'static [HwCpuId]> {
    IPI_SENDER.get().map(|sender| &*sender.hw_cpu_ids)
}

impl IpiSender {
    /// Executes a function on other processors.
    ///
    /// See [`inter_processor_call`] for details. The purpose of exporting this
    /// method is to enable callers to check whether [`IPI_SENDER`] has been
    /// initialized.
    pub(crate) fn inter_processor_call(&self, targets: &CpuSet, call_fn: fn()) -> PendingIpis {
        IPI_SENT.fetch_add(1, Ordering::Relaxed);
        SENT_KIND[kind_of(call_fn)].fetch_add(1, Ordering::Relaxed);
        let irq_guard = irq::disable_local();
        let this_cpu_id = irq_guard.current_cpu();

        // Board-sized staging area for the harts that need an interrupt; the
        // overflowing case falls back to one call per hart.
        const MAX_TARGETS: usize = 64;
        let mut hw_cpu_ids = [self.hw_cpu_ids[0]; MAX_TARGETS];
        let mut num_hw_cpu_ids = 0usize;
        let mut overflow = false;

        let mut call_on_self = false;
        let mut pending_ipis = PendingIpis::new_empty();
        let mut need_ipi = CpuSet::new_empty();
        for cpu_id in targets.iter() {
            if cpu_id == this_cpu_id {
                call_on_self = true;
                continue;
            }
            let mut call_queue = CALL_QUEUES.get_on_cpu(cpu_id).lock();
            call_queue.push_back(call_fn);
            // Set the pending flag before dropping the lock to avoid races.
            //
            // If the flag was already set, that CPU either has an interrupt on
            // the way or is inside the handler right now, and it cannot clear
            // the flag while this lock is held: it clears it under this same
            // lock, and only after finding the queue empty -- which the push
            // above prevents. The callback is therefore guaranteed to be
            // drained, and another interrupt would only cost a firmware call.
            let was_pending = HAS_PENDING_IPIS
                .get_on_cpu(cpu_id)
                .swap(true, Ordering::Release);
            if was_pending {
                IPI_COALESCED.fetch_add(1, Ordering::Relaxed);
            } else {
                need_ipi.add(cpu_id);
                if num_hw_cpu_ids < MAX_TARGETS {
                    hw_cpu_ids[num_hw_cpu_ids] = self.hw_cpu_ids[cpu_id.as_usize()];
                    num_hw_cpu_ids += 1;
                } else {
                    overflow = true;
                }
            }
            pending_ipis.add(cpu_id);
        }
        if !need_ipi.is_empty() {
            if overflow {
                for cpu_id in need_ipi.iter() {
                    crate::arch::irq::send_ipi(self.hw_cpu_ids[cpu_id.as_usize()], &irq_guard as _)
                        .expect("failed to send inter-processor interrupt");
                }
            } else {
                // One SBI call names every target hart that shares a mask
                // word; only the RISC-V backend offers that shape, so other
                // architectures keep the per-target loop.
                #[cfg(target_arch = "riscv64")]
                crate::arch::irq::send_ipi_to_harts(&hw_cpu_ids[..num_hw_cpu_ids], &irq_guard as _)
                    .expect("failed to send inter-processor interrupt");
                #[cfg(not(target_arch = "riscv64"))]
                for cpu_id in need_ipi.iter() {
                    crate::arch::irq::send_ipi(self.hw_cpu_ids[cpu_id.as_usize()], &irq_guard as _)
                        .expect("failed to send inter-processor interrupt");
                }
            }
        }
        if call_on_self {
            // Execute the function synchronously.
            call_fn();
        }
        pending_ipis
    }
}

cpu_local! {
    /// How many inter-processor calls this CPU has handled (diagnostics).
    static IPI_RECEIVED: AtomicU32 = AtomicU32::new(0);
    static CALL_QUEUES: SpinLock<VecDeque<fn()>> = SpinLock::new(VecDeque::new());
    static HAS_PENDING_IPIS: AtomicBool = AtomicBool::new(false);
    static SYNCHRONOUS_CALL_ACK: AtomicBool = AtomicBool::new(true);
}

/// Handles inter-processor calls.
///
/// # Safety
///
/// This function must be called from an IRQ handler that can be triggered by
/// inter-processor interrupts.
pub(crate) unsafe fn do_inter_processor_call(_trapframe: &TrapFrame) {
    note_activity(ACT_IPC);
    note_last_ipi();
    // No races because we are in IRQs.
    let this_cpu_id = crate::cpu::CpuId::current_racy();

    // Counting here is safe; printing here is not, because a thread that was
    // interrupted while writing to the console could hold the lock this CPU
    // would need. The watchdog heartbeat reads these counters instead.
    IPI_RECEIVED
        .get_on_cpu(this_cpu_id)
        .fetch_add(1, Ordering::Relaxed);

    // The lock is held only long enough to take one callback out of the
    // queue. The debug line and the callback itself run with it released:
    // a sender takes this same lock with interrupts disabled, and the sender
    // may be the CPU that kicks the hardware watchdog, so a console write or
    // an arbitrary callback under the lock can stop that heartbeat for long
    // enough that the board resets itself with no message at all.
    loop {
        let call_fn = {
            let mut queue = CALL_QUEUES.get_on_cpu(this_cpu_id).lock();
            let Some(call_fn) = queue.pop_front() else {
                // Clear the pending flag while the queue is still empty and
                // the lock is still held, so a sender pushing next observes
                // it cleared and sets it again.
                HAS_PENDING_IPIS
                    .get_on_cpu(this_cpu_id)
                    .store(false, Ordering::Release);
                return;
            };
            call_fn
        };

        RECEIVED_KIND[kind_of(call_fn)].fetch_add(1, Ordering::Relaxed);

        crate::debug!(
            "Performing inter-processor call to {:#?} on CPU {:#?}",
            call_fn,
            this_cpu_id,
        );
        call_fn();
    }
    note_activity(0);
}

pub(super) fn init() {
    IPI_SENDER.call_once(|| {
        let hw_cpu_ids = crate::boot::smp::construct_hw_cpu_id_mapping();
        IpiSender { hw_cpu_ids }
    });
}

#[cfg(ktest)]
mod test {
    use core::sync::atomic::{AtomicUsize, Ordering};

    use crate::{
        cpu::{self, PinCurrentCpu},
        prelude::ktest,
        task,
    };

    static IPI_CALL_COUNT: AtomicUsize = AtomicUsize::new(0);

    fn count_ipi_call() {
        IPI_CALL_COUNT.fetch_add(1, Ordering::Relaxed);
    }

    #[ktest]
    fn pending_ipis_waits_for_all_cpus() {
        let before = IPI_CALL_COUNT.load(Ordering::Relaxed);

        super::inter_processor_call(&cpu::CpuSet::new_full(), count_ipi_call).wait();

        assert_eq!(
            IPI_CALL_COUNT.load(Ordering::Relaxed) - before,
            cpu::num_cpus()
        );
    }

    #[ktest]
    fn inter_processor_call_runs_on_current_cpu() {
        let preempt_guard = task::disable_preempt();
        let before = IPI_CALL_COUNT.load(Ordering::Relaxed);

        super::inter_processor_call(
            &cpu::CpuSet::from(preempt_guard.current_cpu()),
            count_ipi_call,
        )
        .wait();

        assert_eq!(IPI_CALL_COUNT.load(Ordering::Relaxed) - before, 1);
    }

    #[ktest]
    fn pending_ipis_waits_for_remote_cpu() {
        if cpu::num_cpus() < 2 {
            return;
        }

        let preempt_guard = task::disable_preempt();
        let target_cpu = cpu::all_cpus()
            .find(|cpu_id| *cpu_id != preempt_guard.current_cpu())
            .unwrap();
        let before = IPI_CALL_COUNT.load(Ordering::Relaxed);

        super::inter_processor_call(&cpu::CpuSet::from(target_cpu), count_ipi_call).wait();

        assert_eq!(IPI_CALL_COUNT.load(Ordering::Relaxed) - before, 1);
    }

    #[ktest]
    fn pending_ipis_can_be_extended() {
        if cpu::num_cpus() < 2 {
            return;
        }

        let preempt_guard = task::disable_preempt();
        let target_cpu = cpu::all_cpus()
            .find(|cpu_id| *cpu_id != preempt_guard.current_cpu())
            .unwrap();
        let target_cpus = cpu::CpuSet::from(target_cpu);
        let before = IPI_CALL_COUNT.load(Ordering::Relaxed);

        let mut pending_ipis = super::PendingIpis::new_empty();
        pending_ipis.extend(&super::inter_processor_call(&target_cpus, count_ipi_call));
        pending_ipis.extend(&super::inter_processor_call(&target_cpus, count_ipi_call));
        pending_ipis.wait();

        assert_eq!(IPI_CALL_COUNT.load(Ordering::Relaxed) - before, 2);
    }
}

static SYNCHRONOUS_CALL_LOCK: SpinLock<()> = SpinLock::new(());

fn do_synchronous_memory_barrier() {
    core::sync::atomic::fence(Ordering::SeqCst);

    // No races when reading the CPU ID: the callback runs in interrupt context
    // or synchronously while the caller is pinned by the global call lock.
    let current_cpu = crate::cpu::CpuId::current_racy();
    SYNCHRONOUS_CALL_ACK
        .get_on_cpu(current_cpu)
        .store(true, Ordering::Release);
}

pub fn synchronize_all_cpus() {
    assert!(
        crate::arch::irq::is_local_enabled(),
        "synchronizing CPUs with local IRQs disabled can deadlock"
    );

    // Only one synchronous call may own the per-CPU acknowledgements at once.
    let _call_guard = SYNCHRONOUS_CALL_LOCK.lock();
    let targets = CpuSet::new_full();

    for cpu in targets.iter() {
        SYNCHRONOUS_CALL_ACK
            .get_on_cpu(cpu)
            .store(false, Ordering::Relaxed);
    }

    // Order the caller's accesses before any remote IPI-induced barrier.
    core::sync::atomic::fence(Ordering::SeqCst);

    if let Some(ipi_sender) = IPI_SENDER.get() {
        ipi_sender.inter_processor_call(&targets, do_synchronous_memory_barrier);
    } else {
        // The sender is absent only during uniprocessor bootstrapping.
        assert_eq!(crate::cpu::num_cpus(), 1);
        do_synchronous_memory_barrier();
    }

    for cpu in targets.iter() {
        while !SYNCHRONOUS_CALL_ACK.get_on_cpu(cpu).load(Ordering::Acquire) {
            core::hint::spin_loop();
        }
    }

    // Order all subsequent caller accesses after the acknowledged barriers.
    core::sync::atomic::fence(Ordering::SeqCst);
}
