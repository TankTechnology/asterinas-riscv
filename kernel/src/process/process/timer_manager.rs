// SPDX-License-Identifier: MPL-2.0

use core::time::Duration;

use id_alloc::IdAlloc;
use ostd::{arch, cpu::PrivilegeLevel, irq::InterruptLevel, timer};

use super::Process;
use crate::{
    fs::cgroupfs::{CpuStatKind, charge_cpu_time},
    prelude::*,
    process::{
        posix_thread::AsPosixThread,
        signal::{
            constants::SIGALRM,
            signals::{kernel::KernelSignal, timer::TimerSignalState},
        },
    },
    thread::{
        Thread,
        work_queue::{submit_work_item, work_item::WorkItem},
    },
    time::{
        Timer, TimerManager,
        clocks::{ProfClock, RealTimeClock},
        timer::TimerGuard,
    },
};

#[derive(Clone, Copy)]
pub(crate) enum CpuTimeMode {
    User,
    Kernel,
}

pub(crate) struct CpuTimeAccounting {
    last_tsc: Option<u64>,
    mode: CpuTimeMode,
}

impl CpuTimeAccounting {
    pub(crate) const fn new() -> Self {
        Self {
            last_tsc: None,
            mode: CpuTimeMode::Kernel,
        }
    }

    pub(crate) fn elapsed_to(&mut self, now: u64) -> Option<(CpuTimeMode, Duration)> {
        let last = self.last_tsc.replace(now)?;
        let ticks = now.wrapping_sub(last);
        let nanoseconds = u128::from(ticks)
            .saturating_mul(1_000_000_000)
            .checked_div(u128::from(arch::tsc_freq()))
            .unwrap_or(0);
        let nanoseconds = u64::try_from(nanoseconds).unwrap_or(u64::MAX);
        Some((self.mode, Duration::from_nanos(nanoseconds)))
    }

    pub(crate) fn switch_mode(
        &mut self,
        now: u64,
        mode: CpuTimeMode,
    ) -> Option<(CpuTimeMode, Duration)> {
        let elapsed = self.elapsed_to(now);
        self.mode = mode;
        elapsed
    }

    pub(crate) fn pause(&mut self, now: u64) -> Option<(CpuTimeMode, Duration)> {
        let elapsed = self.elapsed_to(now);
        self.last_tsc = None;
        elapsed
    }

    pub(crate) fn resume_kernel(&mut self, now: u64) {
        debug_assert!(self.last_tsc.is_none());
        self.last_tsc = Some(now);
        self.mode = CpuTimeMode::Kernel;
    }
}

/// Updates the CPU time recorded in the CPU clocks of current Process.
///
/// This function will be invoked at the system timer interrupt, and
/// invoke the callbacks of expired timers which are based on the updated
/// CPU clock.
fn update_cpu_time() {
    // Retrieve some info about the timer interrupt
    let is_kernel_interrupted = {
        let interrupt_level = InterruptLevel::current();
        let InterruptLevel::L1(cpu_priv_at_irq) = interrupt_level else {
            // We are at the interrupt level 2.
            // This means that bottom half of IRQ handling is interrupted.
            // We should not count this time slice on the head of the current task.
            return;
        };
        cpu_priv_at_irq == PrivilegeLevel::Kernel
    };

    // Retrieve some info about the current task
    let Some(current_thread) = Thread::current() else {
        return;
    };
    let Some(posix_thread) = current_thread.as_posix_thread() else {
        return;
    };
    let Some(process) = posix_thread.weak_process().upgrade() else {
        // FIXME: A POSIX thread should have a process but we can have a
        // `None` here.
        return;
    };

    posix_thread.account_cpu_time();

    // Cgroup CPU statistics are still exposed in USER_HZ units. Keep charging
    // one jiffy per periodic sample while the per-thread clocks use precise
    // hardware-counter deltas.
    if is_kernel_interrupted {
        charge_cpu_time(&process, CpuStatKind::System);
    } else {
        charge_cpu_time(&process, CpuStatKind::User);
    }
    posix_thread.process_cpu_timers();
}

/// Registers a function to update the CPU clock in processes and
/// threads during the system timer interrupt.
pub(super) fn init_on_each_cpu() {
    timer::register_callback_on_cpu(update_cpu_time);
}

/// Represents timer resources and utilities for a POSIX process.
pub struct PosixTimerManager {
    /// A real-time countdown timer, measuring in wall clock time.
    alarm_timer: Arc<Timer>,
    /// A timer based on user CPU clock.
    virtual_timer: Arc<Timer>,
    /// A timer based on the profiling clock.
    prof_timer: Arc<Timer>,
    /// An ID allocator to allocate unique timer IDs.
    id_allocator: Mutex<IdAlloc>,
    /// A container managing all POSIX timers created by `timer_create()` syscall
    /// within the process context.
    posix_timers: Mutex<Vec<Option<PosixTimerEntry>>>,
}

#[derive(Clone)]
struct PosixTimerEntry {
    timer: Arc<Timer>,
    signal_state: Arc<TimerSignalState>,
}

fn create_process_timer_callback(
    process_ref: &Weak<Process>,
) -> impl Fn(TimerGuard) + Clone + 'static {
    let current_process = process_ref.clone();
    let sent_signal = move || {
        let signal = KernelSignal::new(SIGALRM);
        if let Some(process) = current_process.upgrade() {
            process.enqueue_signal(Box::new(signal));
        }
    };

    let work_func = Box::new(sent_signal);
    let work_item = WorkItem::new(work_func);

    move |_guard: TimerGuard| {
        submit_work_item(
            work_item.clone(),
            crate::thread::work_queue::WorkPriority::High,
        );
    }
}

impl PosixTimerManager {
    pub(super) fn new(prof_clock: &Arc<ProfClock>, process_ref: &Weak<Process>) -> Self {
        const MAX_NUM_OF_POSIX_TIMERS: usize = 10000;

        let callback = create_process_timer_callback(process_ref);

        let alarm_timer = RealTimeClock::timer_manager().create_timer(callback.clone());

        let virtual_timer =
            TimerManager::new(prof_clock.user_clock().clone()).create_timer(callback.clone());
        let prof_timer = TimerManager::new(prof_clock.clone()).create_timer(callback);

        Self {
            alarm_timer,
            virtual_timer,
            prof_timer,
            id_allocator: Mutex::new(IdAlloc::with_capacity(MAX_NUM_OF_POSIX_TIMERS)),
            posix_timers: Mutex::new(Vec::new()),
        }
    }

    /// Gets the alarm timer of the corresponding process.
    pub fn alarm_timer(&self) -> &Arc<Timer> {
        &self.alarm_timer
    }

    /// Gets the virtual timer of the corresponding process.
    pub fn virtual_timer(&self) -> &Arc<Timer> {
        &self.virtual_timer
    }

    /// Gets the profiling timer of the corresponding process.
    pub fn prof_timer(&self) -> &Arc<Timer> {
        &self.prof_timer
    }

    /// Creates a timer based on the profiling CPU clock of the current process.
    pub fn create_prof_timer<F>(&self, func: F) -> Arc<Timer>
    where
        F: Fn(TimerGuard) + Send + Sync + 'static,
    {
        self.prof_timer.timer_manager().create_timer(func)
    }

    /// Creates a timer based on the user CPU clock of the current process.
    pub fn create_virtual_timer<F>(&self, func: F) -> Arc<Timer>
    where
        F: Fn(TimerGuard) + Send + Sync + 'static,
    {
        self.virtual_timer.timer_manager().create_timer(func)
    }

    /// Allocates an ID and creates a POSIX timer whose callback can safely use
    /// that ID and its signal-delivery state.
    pub fn create_posix_timer<F>(&self, create: F) -> Result<Option<usize>>
    where
        F: FnOnce(usize, Arc<TimerSignalState>) -> Result<Arc<Timer>>,
    {
        let timer_id = {
            let mut timers = self.posix_timers.lock();
            // Holding the lock of `posix_timers` is required to operate the
            // `id_allocator`.
            let Some(timer_id) = self.id_allocator.lock().alloc() else {
                return Ok(None);
            };
            if timers.len() <= timer_id {
                timers.resize(timer_id + 1, None);
            }
            timer_id
        };

        // Timer construction can look up a SIGEV_THREAD_ID target in the PID
        // table. Do not hold the process timer-table lock across that lookup.
        let signal_state = Arc::new(TimerSignalState::new());
        let timer = match create(timer_id, signal_state.clone()) {
            Ok(timer) => timer,
            Err(error) => {
                let _timers = self.posix_timers.lock();
                self.id_allocator.lock().free(timer_id);
                return Err(error);
            }
        };
        let mut timers = self.posix_timers.lock();
        debug_assert!(timers[timer_id].is_none());
        timers[timer_id] = Some(PosixTimerEntry {
            timer,
            signal_state,
        });
        Ok(Some(timer_id))
    }

    /// Finds a POSIX timer by the input `timer_id`.
    pub fn find_posix_timer(&self, timer_id: usize) -> Option<Arc<Timer>> {
        let timers = self.posix_timers.lock();
        if timer_id >= timers.len() {
            return None;
        }

        timers[timer_id].as_ref().map(|entry| entry.timer.clone())
    }

    /// Returns the overrun count associated with the most recently delivered
    /// signal for this POSIX timer.
    pub fn posix_timer_overrun(&self, timer_id: usize) -> Option<i32> {
        let timers = self.posix_timers.lock();
        timers
            .get(timer_id)?
            .as_ref()
            .map(|entry| entry.signal_state.last_overrun())
    }

    /// Removes the POSIX timer with the ID `timer_id`.
    pub fn remove_posix_timer(&self, timer_id: usize) -> Option<Arc<Timer>> {
        let mut timers = self.posix_timers.lock();
        if timer_id >= timers.len() {
            return None;
        }

        let entry = timers[timer_id].take();
        if entry.is_some() {
            // Holding the lock of `posix_timers` is required to operate the `id_allocator`.
            self.id_allocator.lock().free(timer_id);
        }
        entry.map(|entry| entry.timer)
    }
}
