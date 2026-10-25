// SPDX-License-Identifier: MPL-2.0

//! Thread implementation.

use core::sync::atomic::{AtomicBool, AtomicU64, Ordering};

use aster_util::per_cpu_counter::PerCpuCounter;
use ostd::{
    cpu::{AtomicCpuSet, CpuId, CpuSet},
    irq::DisabledLocalIrqGuard,
    sync::SpinLock,
    task::Task,
};

use self::{kernel_thread::AsKernelThread, stats::CONTEXT_SWITCH_COUNTER};
use crate::{
    prelude::*,
    sched::{SchedAttr, SchedPolicy},
};

pub(crate) mod exception;
pub(crate) mod kernel_thread;
pub(crate) mod oops;
mod softirqd;
mod stats;
pub(crate) mod task;
pub(crate) mod work_queue;

pub(crate) use self::stats::collect_context_switch_count;

/// Spawns a kernel thread that runs `func` on the kernel scheduler.
///
/// The thread has no user address space and never returns to user mode: the
/// wrapper exits it when `func` returns. Components use this for deferred work
/// that may sleep (for example, completing a DRM page flip outside the timer
/// softirq). `name` is for diagnostics only.
pub fn spawn_kernel_thread(name: &'static str, func: impl FnOnce() + Send + 'static) {
    let _ = name;
    kernel_thread::ThreadOptions::new(func).spawn();
}

pub(crate) type Tid = u32;

fn pre_schedule_handler(irq_guard: &DisabledLocalIrqGuard) {
    let Some(task) = Task::current() else {
        return;
    };
    let Some(thread_local) = task.as_thread_local() else {
        return;
    };

    thread_local.supp_user_context().before_schedule(irq_guard);
}

fn post_schedule_handler() {
    // No races because preemption shouldn't happen in pre-/post-schedule handlers.
    CONTEXT_SWITCH_COUNTER
        .get()
        .unwrap()
        .add_on_cpu(CpuId::current_racy(), 1);

    let task = Task::current().unwrap();
    if let Some(thread_local) = task.as_thread_local() {
        let vmar = thread_local.vmar().borrow();
        if let Some(vmar) = vmar.as_ref() {
            vmar.vm_space().activate()
        }
    } else if let Some(vmar) = task
        .as_kernel_thread()
        .and_then(|kernel_thread| kernel_thread.vmar())
    {
        vmar.vm_space().activate();
    }
}

pub(super) fn init() {
    CONTEXT_SWITCH_COUNTER.call_once(PerCpuCounter::new);
    ostd::task::inject_pre_schedule_handler(pre_schedule_handler);
    ostd::task::inject_post_schedule_handler(post_schedule_handler);
    ostd::mm::fault::inject_user_page_fault_handler(exception::page_fault_handler);
}

pub(super) fn init_in_first_kthread() {
    work_queue::init_in_first_kthread();
    softirqd::init_in_first_kthread();
}

/// The last syscall entered by a thread, for `/proc/<pid>/syscall`.
#[derive(Clone, Copy, Debug)]
pub(crate) struct SyscallState {
    /// Whether the thread is currently inside a syscall.
    pub active: bool,
    /// Syscall number.
    pub nr: u64,
    /// Syscall arguments.
    pub args: [u64; 6],
    /// User stack pointer at syscall entry.
    pub sp: u64,
    /// User instruction pointer at syscall entry.
    pub pc: u64,
}

impl Default for SyscallState {
    fn default() -> Self {
        Self {
            active: false,
            nr: 0,
            args: [0; 6],
            sp: 0,
            pc: 0,
        }
    }
}

/// A thread is a wrapper on top of a task.
#[derive(Debug)]
pub(crate) struct Thread {
    // Immutable part:
    //
    /// Low-level task.
    task: Weak<Task>,
    /// POSIX thread information or kernel thread information.
    data: Box<dyn Send + Sync + Any>,

    // Mutable part:
    //
    /// Thread status.
    is_exited: AtomicBool,
    /// Thread CPU affinity.
    cpu_affinity: AtomicCpuSet,
    /// Thread scheduling attribute.
    sched_attr: SchedAttr,
    /// Minor page faults handled for this thread (no I/O required).
    min_flt: AtomicU64,
    /// Major page faults handled for this thread (I/O required).
    maj_flt: AtomicU64,
    /// Last syscall entered by this thread (sampled by procfs).
    syscall_state: SpinLock<SyscallState>,
}

impl Thread {
    /// Never call these function directly
    pub(crate) fn new(
        task: Weak<Task>,
        data: impl Send + Sync + Any,
        cpu_affinity: CpuSet,
        sched_policy: SchedPolicy,
    ) -> Self {
        Thread {
            task,
            data: Box::new(data),
            is_exited: AtomicBool::new(false),
            cpu_affinity: AtomicCpuSet::new(cpu_affinity),
            sched_attr: SchedAttr::new(sched_policy),
            min_flt: AtomicU64::new(0),
            maj_flt: AtomicU64::new(0),
            syscall_state: SpinLock::new(SyscallState::default()),
        }
    }

    /// Returns the current thread.
    ///
    /// This function returns `None` if the current task is not associated with
    /// a thread, or if called within the bootstrap context.
    pub(crate) fn current() -> Option<Arc<Self>> {
        Task::current()?.as_thread().cloned()
    }

    /// Returns the task associated with this thread.
    pub(crate) fn task(&self) -> Arc<Task> {
        self.task.upgrade().unwrap()
    }

    /// Runs this thread at once.
    #[track_caller]
    pub(crate) fn run(&self) {
        self.task.upgrade().unwrap().run();
    }

    /// Records one handled page fault.
    pub(crate) fn record_page_fault(&self, major: bool) {
        if major {
            self.maj_flt.fetch_add(1, Ordering::Relaxed);
        } else {
            self.min_flt.fetch_add(1, Ordering::Relaxed);
        }
    }

    /// Returns the number of minor page faults handled for this thread.
    pub(crate) fn min_flt(&self) -> u64 {
        self.min_flt.load(Ordering::Relaxed)
    }

    /// Returns the number of major page faults handled for this thread.
    pub(crate) fn maj_flt(&self) -> u64 {
        self.maj_flt.load(Ordering::Relaxed)
    }

    /// Records that the thread entered a syscall.
    pub(crate) fn record_syscall_entry(&self, nr: u64, args: [u64; 6], sp: u64, pc: u64) {
        *self.syscall_state.lock() = SyscallState {
            active: true,
            nr,
            args,
            sp,
            pc,
        };
    }

    /// Records that the thread left its syscall.
    pub(crate) fn record_syscall_exit(&self) {
        self.syscall_state.lock().active = false;
    }

    /// Returns a snapshot of the thread's syscall state.
    pub(crate) fn syscall_state(&self) -> SyscallState {
        *self.syscall_state.lock()
    }

    /// Returns whether the thread is exited.
    pub(crate) fn is_exited(&self) -> bool {
        self.is_exited.load(Ordering::Acquire)
    }

    pub(super) fn exit(&self) {
        self.is_exited.store(true, Ordering::Release);
    }

    /// Returns the reference to the atomic CPU affinity.
    pub(crate) fn atomic_cpu_affinity(&self) -> &AtomicCpuSet {
        &self.cpu_affinity
    }

    pub(crate) fn sched_attr(&self) -> &SchedAttr {
        &self.sched_attr
    }

    /// Yields the execution to another thread.
    ///
    /// This method will return once the current thread is scheduled again.
    #[track_caller]
    pub(crate) fn yield_now() {
        Task::yield_now()
    }

    /// Joins the execution of the thread.
    ///
    /// This method will return after the thread exits.
    #[cfg_attr(not(ktest), expect(dead_code))]
    #[track_caller]
    pub(crate) fn join(&self) {
        while !self.is_exited() {
            Self::yield_now();
        }
    }

    /// Returns the associated data.
    pub(crate) fn data(&self) -> &(dyn Send + Sync + Any) {
        &*self.data
    }
}

/// A trait to provide the `as_thread` method for tasks.
pub(crate) trait AsThread {
    /// Returns the associated [`Thread`].
    fn as_thread(&self) -> Option<&Arc<Thread>>;
}

impl AsThread for Task {
    fn as_thread(&self) -> Option<&Arc<Thread>> {
        self.data().downcast_ref::<Arc<Thread>>()
    }
}
