// SPDX-License-Identifier: MPL-2.0

use core::sync::atomic::{AtomicU32, AtomicU64, Ordering};

use aster_rights::{ReadDupOp, ReadOp, ReadWriteOp};
use ostd::{
    sync::{RoArc, RwMutexReadGuard, Waker},
    task::Task,
};
use spin::Once;

use super::{
    Credentials, Process,
    signal::{sig_mask::AtomicSigMask, sig_num::SigNum, sig_queues::SigQueues, signals::Signal},
};
use crate::{
    events::IoEvents,
    fs::{file::file_table::FileTable, thread_info::ThreadFsInfo},
    prelude::*,
    process::{
        ExitCode, Pid,
        namespace::nsproxy::NsProxy,
        posix_thread::ptrace::TraceeStatus,
        signal::{PauseReason, PollHandle, sig_mask::SigMask},
    },
    syscall::SockFilter,
    thread::{Thread, Tid},
    time::{Timer, TimerManager, clocks::ProfClock, timer::TimerGuard},
};

pub mod alien_access;
mod builder;
mod cpu_sync;
mod exit;
pub mod futex;
mod name;
mod personality;
mod posix_thread_ext;
pub mod ptrace;
mod robust_list;
mod rseq;
mod thread_local;

pub use builder::PosixThreadBuilder;
pub(super) use exit::sigkill_other_threads;
pub use exit::{do_exit, do_exit_group};
pub use name::{MAX_THREAD_NAME_LEN, ThreadName};
pub use personality::Personality;
pub use posix_thread_ext::AsPosixThread;
pub use robust_list::RobustListHead;
pub use rseq::{
    RSEQ_ALIGN, RSEQ_CPU_ID_OFFSET, RSEQ_CPU_ID_UNINITIALIZED, RSEQ_FLAG_UNREGISTER, RSEQ_MIN_SIZE,
    RSEQ_SIG_OFFSET, Rseq,
};
pub use thread_local::{AsThreadLocal, FileTableRefMut, ThreadLocal};

/// An immutable node in a thread's seccomp filter tree.
///
/// Nodes are identity-bearing: installing identical BPF programs twice still
/// creates distinct nodes, matching Linux's filter-tree ancestry semantics.
#[derive(Debug)]
pub struct SeccompFilter {
    program: Arc<[SockFilter]>,
    parent: Option<Arc<SeccompFilter>>,
    path_instructions: usize,
}

impl SeccompFilter {
    /// Linux limits a filter path to 32K instructions. Each existing node
    /// contributes an additional four-instruction accounting overhead when a
    /// new node is appended.
    pub const MAX_INSNS_PER_PATH: usize = 1 << 15;

    pub fn try_new(
        program: Arc<[SockFilter]>,
        parent: Option<Arc<SeccompFilter>>,
    ) -> Option<Arc<Self>> {
        let ancestor_cost = match parent.as_ref() {
            Some(parent) => parent.path_instructions.checked_add(4)?,
            None => 0,
        };
        let path_instructions = ancestor_cost.checked_add(program.len())?;
        if path_instructions > Self::MAX_INSNS_PER_PATH {
            return None;
        }
        Some(Arc::new(Self {
            program,
            parent,
            path_instructions,
        }))
    }

    pub fn program(&self) -> &[SockFilter] {
        &self.program
    }

    pub fn parent(&self) -> Option<&Arc<SeccompFilter>> {
        self.parent.as_ref()
    }

    /// Returns whether `ancestor` is the root of, or a node in, `descendant`.
    /// The empty chain is the root ancestor of every chain.
    pub fn is_ancestor(
        ancestor: Option<&Arc<SeccompFilter>>,
        descendant: Option<&Arc<SeccompFilter>>,
    ) -> bool {
        let Some(ancestor) = ancestor else {
            return true;
        };
        let mut current = descendant;
        while let Some(node) = current {
            if Arc::ptr_eq(ancestor, node) {
                return true;
            }
            current = node.parent();
        }
        false
    }
}

impl Drop for SeccompFilter {
    fn drop(&mut self) {
        // Arc's normal recursive destruction can exhaust the kernel stack for
        // a long uniquely-owned chain. Iteratively peel unique ancestors;
        // stop as soon as an ancestor is shared by another thread/snapshot.
        let mut parent = self.parent.take();
        while let Some(parent_arc) = parent {
            let Ok(mut parent_node) = Arc::try_unwrap(parent_arc) else {
                break;
            };
            parent = parent_node.parent.take();
        }
    }
}

pub struct PosixThread {
    // Immutable part
    process: Weak<Process>,
    task: Weak<Task>,

    // Mutable part
    tid: AtomicU32,

    name: Mutex<ThreadName>,

    /// Process credentials. At the kernel level, credentials are a per-thread attribute.
    credentials: Credentials,

    /// The file system information of the thread.
    fs: RwMutex<Arc<ThreadFsInfo>>,

    // Files
    /// File table
    file_table: Mutex<Option<RoArc<FileTable>>>,

    // Signal
    /// Blocked signals
    sig_mask: AtomicSigMask,
    /// Thread-directed sigqueue
    sig_queues: SigQueues,
    /// The per-thread signal [`Waker`], which will be used to wake up the thread
    /// when enqueuing a signal, along with the reason why the thread is paused.
    signalled_waker: SpinLock<Option<(Arc<Waker>, PauseReason)>>,

    // Time
    /// A profiling clock measures the user CPU time and kernel CPU time in the thread.
    prof_clock: Arc<ProfClock>,
    /// A manager that manages timers based on the user CPU time of the current thread.
    virtual_timer_manager: Arc<TimerManager>,
    /// A manager that manages timers based on the profiling clock of the current thread.
    prof_timer_manager: Arc<TimerManager>,

    /// I/O Scheduling priority value
    io_priority: AtomicU32,

    /// The namespaces that the thread belongs to.
    ns_proxy: Mutex<Option<Arc<NsProxy>>>,

    /// The current timer slack value for this thread.
    timer_slack_ns: AtomicU64,
    /// The default timer slack value for this thread.
    default_timer_slack_ns: AtomicU64,

    // Ptrace
    /// Status of being traced.
    tracee_status: Once<TraceeStatus>,
    /// Threads traced by this thread.
    tracees: Once<Mutex<BTreeMap<Tid, Arc<Thread>>>>,

    /// Exit code of this thread.
    exit_code: AtomicU32,

    /// The personality value for this thread.
    personality: AtomicU32,

    /// The seccomp mode of this thread (`0` = disabled, `1` = strict,
    /// `2` = filter; see `crate::syscall::seccomp`).
    seccomp_mode: AtomicU32,

    /// The immutable seccomp BPF filter chain.
    /// Only meaningful when `seccomp_mode == SECCOMP_MODE_FILTER`.
    seccomp_filter: Mutex<Option<Arc<SeccompFilter>>>,
}

impl PosixThread {
    pub fn process(&self) -> Arc<Process> {
        self.process.upgrade().unwrap()
    }

    pub fn weak_process(&self) -> &Weak<Process> {
        &self.process
    }

    /// Returns the thread id
    pub fn tid(&self) -> Tid {
        self.tid.load(Ordering::Relaxed)
    }

    /// Sets the thread as the main thread by changing its thread ID.
    pub(super) fn set_main(&self, pid: Pid) {
        debug_assert_eq!(pid, self.process.upgrade().unwrap().pid());
        debug_assert_ne!(pid, self.tid.load(Ordering::Relaxed));

        self.tid.store(pid, Ordering::Relaxed);
    }

    pub fn thread_name(&self) -> &Mutex<ThreadName> {
        &self.name
    }

    /// Returns a read guard to the filesystem information of the thread.
    pub fn read_fs(&self) -> RwMutexReadGuard<'_, Arc<ThreadFsInfo>> {
        self.fs.read()
    }

    /// Sets the filesystem information of the thread.
    pub(in crate::process) fn set_fs(&self, new_fs: Arc<ThreadFsInfo>) {
        let mut fs_lock = self.fs.write();
        *fs_lock = new_fs;
    }

    pub fn file_table(&self) -> &Mutex<Option<RoArc<FileTable>>> {
        &self.file_table
    }

    /// Returns the signal mask of the thread.
    pub fn sig_mask(&self) -> SigMask {
        self.sig_mask.load(Ordering::Relaxed)
    }

    pub(super) fn sig_queues(&self) -> &SigQueues {
        &self.sig_queues
    }

    /// Returns whether the signal is blocked by the thread.
    pub fn has_signal_blocked(&self, signum: SigNum) -> bool {
        // FIXME: Some signals cannot be blocked, even set in sig_mask.
        self.sig_mask.contains(signum, Ordering::Relaxed)
    }

    /// Sets the input [`Waker`] as the signalled waker of this thread,
    /// along with the reason why the thread is paused.
    ///
    /// This approach can collaborate with signal-aware wait methods.
    /// Once a signalled waker is set for a thread, it cannot be reset until it is cleared.
    ///
    /// # Panics
    ///
    /// If setting a new waker before clearing the current thread's signalled waker
    /// this method will panic.
    pub fn set_signalled_waker(&self, waker: Arc<Waker>, reason: PauseReason) {
        let mut signalled_waker = self.signalled_waker.lock();
        assert!(signalled_waker.is_none());
        *signalled_waker = Some((waker, reason));
    }

    /// Clears the signalled waker of this thread.
    pub fn clear_signalled_waker(&self) {
        *self.signalled_waker.lock() = None;
    }

    /// Returns the sleeping state of this thread.
    pub fn sleeping_state(&self) -> SleepingState {
        // This implementation prevents a thread (let's call it `threadA`) that is
        // sleeping in an interruptible wait from being mistakenly reported as
        // sleeping in an uninterruptible wait due to a race condition, where another
        // thread (`threadB`) may observe that its `task.schedule_info().cpu` is
        // `AtomicCpuId::NONE` and its `signalled_waker` is `None` (not set yet or
        // already cleared).
        //
        // When `threadA` enters an interruptible wait, it executes the following steps:
        // ```
        // A1: Acquire signalled_waker.lock |
        // A2: set signalled_waker to Some  |-- critical section #1
        // A3: Release signalled_waker.lock |
        // A4: cpu.set_to_none(Relaxed)
        // A5: cpu.set_if_is_none(cpuid, Relaxed)
        // A6: Acquire signalled_waker.lock |
        // A7: set signalled_waker to None  |-- critical section #2
        // A8: Release signalled_waker.lock |
        // ```
        //
        // When `threadB` calls `threadA.sleeping_state()`, it executes the following steps:
        // ```
        // B1: Acquire threadA.signalled_waker.lock |
        // B2: check threadA.signalled_waker        |-- critical section #3
        // B3: check threadA.cpu.get(Relaxed)       |
        // B4: Release threadA.signalled_waker.lock |
        // ```
        //
        // We can see that:
        //  - If #3 happens before #1, B3 can not observe the effect of A4 due to the
        //    release-acquire pair B4-A1.
        //  - If #3 happens between #1 and #2, B2 will always see a `Some`.
        //  - If #3 happens after #2, B3 can observe the effect of A5 due to the
        //    release-acquire pair A8-B1.
        // Therefore, the condition where both B2 and B3 see `None` will never happen.
        //
        // Similarly, this implementation prevents a process that has been stopped by
        // a signal or ptrace from being incorrectly reported as sleeping in an
        // (un)interruptible wait.
        //
        // FIXME: This implementation cannot prevent a stopped process from being
        // reported as running when `crate::process::signal::handle_pending_signal`
        // is called, but the pending signal is not a `SIGCONT`. However, is this
        // actually a problem? We considered an approach to fix this issue, but it
        // does not fully resolve it and has some drawbacks. For more details, see
        // <https://github.com/asterinas/asterinas/pull/2491#issuecomment-3527958970>.
        let signalled_waker = self.signalled_waker.lock();
        let task = self.task.upgrade().unwrap();
        match (
            signalled_waker.as_ref(),
            task.schedule_info().cpu.get().is_none(),
        ) {
            (Some((_, PauseReason::Sleep)), true) => SleepingState::Interruptible,
            (Some((_, PauseReason::StopBySignal)), true) => SleepingState::StopBySignal,
            (Some((_, PauseReason::StopByPtrace)), true) => SleepingState::StopByPtrace,
            (None, true) => SleepingState::Uninterruptible,
            (_, false) => SleepingState::Running,
        }
    }

    /// Wakes up the signalled waker.
    pub fn wake_signalled_waker(&self) {
        if let Some((waker, _)) = &*self.signalled_waker.lock() {
            waker.wake_up();
        }
    }

    /// Enqueues a thread-directed signal.
    ///
    /// This method does not perform permission checks on user signals.
    /// Therefore, unless the caller can ensure that there are no permission issues,
    /// this method should be used to enqueue kernel signals or fault signals.
    pub fn enqueue_signal(&self, signal: Box<dyn Signal>) {
        self.sig_queues.enqueue(signal);
        self.wake_signalled_waker();
    }

    pub fn register_signalfd_poller(&self, poller: &mut PollHandle, mask: IoEvents) {
        self.sig_queues.register_signalfd_poller(poller, mask);
        self.process()
            .sig_queues()
            .register_signalfd_poller(poller, mask);
    }

    /// Returns a reference to the profiling clock of the current thread.
    pub fn prof_clock(&self) -> &Arc<ProfClock> {
        &self.prof_clock
    }

    /// Creates a timer based on the profiling CPU clock of the current thread.
    pub fn create_prof_timer<F>(&self, func: F) -> Arc<Timer>
    where
        F: Fn(TimerGuard) + Send + Sync + 'static,
    {
        self.prof_timer_manager.create_timer(func)
    }

    /// Creates a timer based on the user CPU clock of the current thread.
    pub fn create_virtual_timer<F>(&self, func: F) -> Arc<Timer>
    where
        F: Fn(TimerGuard) + Send + Sync + 'static,
    {
        self.virtual_timer_manager.create_timer(func)
    }

    /// Checks the `TimerCallback`s that are managed by the `prof_timer_manager`.
    /// If any have timed out, call the corresponding callback functions.
    pub fn process_expired_timers(&self) {
        self.prof_timer_manager.process_expired_timers();
    }

    /// Gets the read-only credentials of the thread.
    pub fn credentials(&self) -> Credentials<ReadOp> {
        self.credentials.dup().restrict()
    }

    /// Gets the duplicatable read-only credentials of the thread.
    pub fn credentials_dup(&self) -> Credentials<ReadDupOp> {
        self.credentials.dup().restrict()
    }

    /// Irreversibly enables `no_new_privs` for this thread.
    ///
    /// This narrow internal API is used by seccomp TSYNC, which must propagate
    /// the caller's `no_new_privs` bit to every synchronized sibling.
    pub(crate) fn set_no_new_privs(&self) {
        self.credentials.set_no_new_privs();
    }

    /// Returns the I/O priority value of the thread.
    pub fn io_priority(&self) -> &AtomicU32 {
        &self.io_priority
    }

    /// Returns the namespaces which the thread belongs to.
    pub fn ns_proxy(&self) -> &Mutex<Option<Arc<NsProxy>>> {
        &self.ns_proxy
    }

    /// Returns the current timer slack value in nanoseconds.
    pub fn timer_slack_ns(&self) -> u64 {
        self.timer_slack_ns.load(Ordering::Relaxed)
    }

    /// Sets the current timer slack value in nanoseconds.
    pub fn set_timer_slack_ns(&self, slack_ns: u64) {
        self.timer_slack_ns.store(slack_ns, Ordering::Relaxed);
    }

    /// Resets the current timer slack to the default value.
    pub fn reset_timer_slack_to_default(&self) {
        let default = self.default_timer_slack_ns.load(Ordering::Relaxed);
        self.timer_slack_ns.store(default, Ordering::Relaxed);
    }

    /// Sets the exit code of this thread.
    pub(super) fn set_exit_code(&self, exit_code: ExitCode) {
        self.exit_code.store(exit_code, Ordering::Relaxed);
    }

    /// Returns the exit code of this thread.
    pub fn exit_code(&self) -> ExitCode {
        self.exit_code.load(Ordering::Relaxed)
    }

    /// Returns the seccomp mode of this thread.
    pub fn seccomp_mode(&self) -> u32 {
        self.seccomp_mode.load(Ordering::Relaxed)
    }

    /// Sets the seccomp mode of this thread.
    ///
    /// As on Linux, entering seccomp mode is irreversible for the lifetime of
    /// the thread; this method is only called from `seccomp(2)`.
    pub fn set_seccomp_mode(&self, mode: u32) {
        self.seccomp_mode.store(mode, Ordering::Relaxed);
    }

    /// Returns the head of the seccomp filter chain, if one is installed.
    pub fn seccomp_filter(&self) -> Option<Arc<SeccompFilter>> {
        self.seccomp_filter.lock().clone()
    }

    /// Installs a new head for the seccomp filter chain.
    ///
    /// As with [`Self::set_seccomp_mode`], this is only called from `seccomp(2)`
    /// and is irreversible for the lifetime of the thread.
    pub fn set_seccomp_filter(&self, filter: Arc<SeccompFilter>) {
        *self.seccomp_filter.lock() = Some(filter);
    }
}

#[cfg(ktest)]
mod seccomp_filter_tests {
    use ostd::prelude::ktest;

    use super::*;

    fn program(len: usize) -> Arc<[SockFilter]> {
        Arc::from(
            vec![
                SockFilter {
                    code: 0x06, // BPF_RET | BPF_K
                    jt: 0,
                    jf: 0,
                    k: 0x7fff_0000, // SECCOMP_RET_ALLOW
                };
                len
            ]
            .into_boxed_slice(),
        )
    }

    #[ktest]
    fn seccomp_filter_path_budget_boundary() {
        let mut chain = None;
        for _ in 0..7 {
            chain = SeccompFilter::try_new(program(4096), chain);
            assert!(chain.is_some());
        }
        chain = SeccompFilter::try_new(program(4068), chain);
        assert_eq!(chain.as_ref().unwrap().path_instructions, 1 << 15);
        assert!(SeccompFilter::try_new(program(1), chain.clone()).is_none());
    }

    #[ktest]
    fn dropping_long_unique_filter_chain_is_iterative() {
        let one_instruction = program(1);
        let mut chain = None;
        // 6554 one-insn nodes cost 6554 + 6553*4 = 32766.
        for _ in 0..6554 {
            chain = SeccompFilter::try_new(one_instruction.clone(), chain);
            assert!(chain.is_some());
        }
        assert!(SeccompFilter::try_new(one_instruction, chain.clone()).is_none());
        drop(chain);
    }
}

/// Provides administrative APIs for the current POSIX thread.
pub trait ContextPthreadAdminApi {
    /// Sets the signal mask of the current thread.
    ///
    /// Note that it is not possible to block SIGKILL or SIGSTOP.
    /// Attempts to do so are silently ignored.
    fn set_sig_mask(&self, sig_mask: SigMask);

    /// Saves and sets the signal mask of the current thread.
    ///
    /// If there are no signals to process, the old signal mask will
    /// be automatically restored when returning from the system call.
    /// Otherwise, it will be restored after the signal handler.
    ///
    /// This method should only be called when handling a system call.
    /// It should not be called more than once per system call.
    fn save_and_set_sig_mask(&self, sig_mask: SigMask);

    /// Gets the read-write credentials of the current thread.
    fn credentials_mut(&self) -> Credentials<ReadWriteOp>;
}

impl ContextPthreadAdminApi for Context<'_> {
    fn set_sig_mask(&self, mut sig_mask: SigMask) {
        use crate::process::signal::constants::{SIGKILL, SIGSTOP};

        sig_mask -= SIGKILL;
        sig_mask -= SIGSTOP;

        self.posix_thread
            .sig_mask
            .store(sig_mask, Ordering::Relaxed);
    }

    fn save_and_set_sig_mask(&self, sig_mask: SigMask) {
        let sig_mask_saved = self.thread_local.sig_mask_saved();
        debug_assert!(sig_mask_saved.get().is_none());
        sig_mask_saved.set(Some(self.posix_thread.sig_mask()));

        self.set_sig_mask(sig_mask);
    }

    fn credentials_mut(&self) -> Credentials<ReadWriteOp> {
        self.posix_thread.credentials.dup().restrict()
    }
}

/// The TID of the first POSIX thread (i.e., the main thread of the init process).
pub const FIRST_POSIX_TID: Tid = 1;

static POSIX_TID_ALLOCATOR: AtomicU32 = AtomicU32::new(FIRST_POSIX_TID);

/// Allocates a new TID for the new POSIX thread.
pub fn allocate_posix_tid() -> Tid {
    let tid = POSIX_TID_ALLOCATOR.fetch_add(1, Ordering::Relaxed);
    if tid >= PID_MAX {
        // When the kernel's next PID value reaches `PID_MAX`,
        // it should wrap back to a minimum PID value.
        // PIDs with a value of `PID_MAX` or larger should not be allocated.
        // Reference: <https://docs.kernel.org/admin-guide/sysctl/kernel.html#pid-max>.
        //
        // FIXME: Currently, we cannot determine which PID is recycled,
        // so we are unable to allocate smaller PIDs.
        warn!("the allocated ID is greater than the maximum allowed PID");
    }
    tid
}

/// Returns the last allocated TID.
pub fn last_tid() -> Tid {
    POSIX_TID_ALLOCATOR.load(Ordering::Relaxed) - 1
}

/// The maximum allowed process ID.
//
// FIXME: The current value is chosen arbitrarily.
// This value can be modified by the user by writing to `/proc/sys/kernel/pid_max`.
pub const PID_MAX: u32 = u32::MAX / 2;

/// The sleeping state of a thread.
#[derive(Clone, Copy, Debug)]
pub enum SleepingState {
    /// The thread is running.
    Running,
    /// The thread is sleeping in an interruptible wait.
    Interruptible,
    /// The thread is sleeping in an uninterruptible wait.
    Uninterruptible,
    /// The thread is stopped by a signal.
    StopBySignal,
    /// The thread is stopped by ptrace.
    StopByPtrace,
}
