// SPDX-License-Identifier: MPL-2.0

//! Symmetric Multi-Processing (SMP) support.
//!
//! This module provides a way to execute code on other processors via inter-
//! processor interrupts.

use alloc::{boxed::Box, collections::VecDeque};
use core::sync::atomic::{AtomicBool, Ordering};

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
/// The provided function `f` will be executed on all target processors
/// specified by `targets`. It can also be executed on the current processor.
/// The function should be short and non-blocking, as it will be executed in
/// interrupt context with interrupts disabled.
///
/// This function does not block until all the target processors acknowledges
/// the interrupt. So if any of the target processors disables IRQs for too
/// long that the controller cannot queue them, the function will not be
/// executed.
///
/// The function `f` will be executed asynchronously on the target processors.
/// However if called on the current processor, it will be synchronous.
pub fn inter_processor_call(targets: &CpuSet, f: fn()) {
    let ipi_sender = IPI_SENDER.get().unwrap();
    ipi_sender.inter_processor_call(targets, f);
}

/// Executes a full memory barrier synchronously on every online CPU.
///
/// Unlike [`inter_processor_call`], this function does not return until every
/// target CPU has acknowledged the callback. Callers must have local IRQs
/// enabled so that two CPUs issuing synchronous calls at the same time cannot
/// deadlock while handling each other's IPIs.
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

/// A sender that carries necessary information to send inter-processor interrupts.
///
/// The purpose of exporting this type is to enable the users to check whether
/// [`IPI_SENDER`] has been initialized.
pub(crate) struct IpiSender {
    hw_cpu_ids: Box<[HwCpuId]>,
}

/// The [`IpiSender`] singleton.
pub(crate) static IPI_SENDER: Once<IpiSender> = Once::new();

impl IpiSender {
    /// Executes a function on other processors.
    ///
    /// See [`inter_processor_call`] for details. The purpose of exporting this
    /// method is to enable callers to check whether [`IPI_SENDER`] has been
    /// initialized.
    pub(crate) fn inter_processor_call(&self, targets: &CpuSet, f: fn()) {
        let irq_guard = irq::disable_local();
        let this_cpu_id = irq_guard.current_cpu();

        let mut call_on_self = false;
        for cpu_id in targets.iter() {
            if cpu_id == this_cpu_id {
                call_on_self = true;
                continue;
            }
            CALL_QUEUES.get_on_cpu(cpu_id).lock().push_back(f);
        }
        for cpu_id in targets.iter() {
            if cpu_id == this_cpu_id {
                continue;
            }
            let hw_cpu_id = self.hw_cpu_ids[cpu_id.as_usize()];
            crate::arch::irq::send_ipi(hw_cpu_id, &irq_guard as _);
        }
        if call_on_self {
            // Execute the function synchronously.
            f();
        }
    }
}

cpu_local! {
    static CALL_QUEUES: SpinLock<VecDeque<fn()>> = SpinLock::new(VecDeque::new());
    static SYNCHRONOUS_CALL_ACK: AtomicBool = AtomicBool::new(true);
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

/// Handles inter-processor calls.
///
/// # Safety
///
/// This function must be called from an IRQ handler that can be triggered by
/// inter-processor interrupts.
pub(crate) unsafe fn do_inter_processor_call(_trapframe: &TrapFrame) {
    // No races because we are in IRQs.
    let this_cpu_id = crate::cpu::CpuId::current_racy();

    let mut queue = CALL_QUEUES.get_on_cpu(this_cpu_id).lock();
    while let Some(f) = queue.pop_front() {
        crate::debug!(
            "Performing inter-processor call to {:#?} on CPU {:#?}",
            f,
            this_cpu_id,
        );
        f();
    }
}

pub(super) fn init() {
    IPI_SENDER.call_once(|| {
        let hw_cpu_ids = crate::boot::smp::construct_hw_cpu_id_mapping();
        IpiSender { hw_cpu_ids }
    });
}

/// Returns the hardware CPU ID mapping used for inter-processor calls.
#[cfg(target_arch = "riscv64")]
pub(crate) fn hw_cpu_id_mapping() -> Option<&'static [HwCpuId]> {
    IPI_SENDER.get().map(|sender| &*sender.hw_cpu_ids)
}
