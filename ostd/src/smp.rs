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
use core::sync::atomic::{AtomicBool, AtomicU32, Ordering};

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

/// Pending remote inter-processor calls.
pub struct PendingIpis {
    cpus: CpuSet,
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
        for cpu_id in self.cpus.iter() {
            // Wait until there are no pending IPIs on the target CPU.
            //
            // Note that if new IPIs arrive to that CPU in the meantime, we
            // will also wait for them. This is fine because there usually
            // aren't too many IPIs in common cases.
            while HAS_PENDING_IPIS.get_on_cpu(cpu_id).load(Ordering::Acquire) {
                core::hint::spin_loop();
            }
        }
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

        let mut call_on_self = false;
        let mut pending_ipis = PendingIpis::new_empty();
        for cpu_id in targets.iter() {
            if cpu_id == this_cpu_id {
                call_on_self = true;
                continue;
            }
            let mut call_queue = CALL_QUEUES.get_on_cpu(cpu_id).lock();
            call_queue.push_back(call_fn);
            // Set the pending flag before dropping the lock to avoid races.
            HAS_PENDING_IPIS
                .get_on_cpu(cpu_id)
                .store(true, Ordering::Release);
            pending_ipis.add(cpu_id);
        }
        for cpu_id in targets.iter() {
            if cpu_id == this_cpu_id {
                continue;
            }
            let hw_cpu_id = self.hw_cpu_ids[cpu_id.as_usize()];
            crate::arch::irq::send_ipi(hw_cpu_id, &irq_guard as _)
                .expect("failed to send inter-processor interrupt");
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
