// SPDX-License-Identifier: MPL-2.0

use core::{
    sync::atomic::{AtomicBool, AtomicI16, AtomicU32, Ordering},
    time::Duration,
};

use self::timer_manager::PosixTimerManager;
use super::{
    namespace::pid_ns::{PidNamespace, PidNsReservation},
    pid_table::{self, PidTable},
    posix_thread::{AsPosixThread, FIRST_POSIX_TID, PosixThread},
    process_vm::ProcessVmarGuard,
    rlimit::ResourceLimits,
    signal::{
        c_types::siginfo_t,
        constants::{
            CLD_CONTINUED, CLD_STOPPED, SIGCHLD, SIGCONT, SIGKILL, SIGSTOP, SIGTSTP, SIGTTIN,
            SIGTTOU,
        },
        job_control::{GroupStopParticipant, SignalJobControl, is_stop_signal},
        sig_action::{SigActionFlags, SigHandler},
        sig_disposition::SigDispositions,
        sig_mask::SigSet,
        sig_num::{AtomicSigNum, SigNum},
        signals::{Signal, raw::RawSignal},
    },
    status::ProcessStatus,
    task_set::TaskSet,
};
use crate::{
    events::IoEvents,
    fs::cgroupfs::CgroupNode,
    prelude::*,
    process::{
        UserNamespace, WaitOptions,
        signal::{Pollee, sig_queues::SigQueues},
        status::StopWaitStatus,
    },
    sched::{AtomicNice, Nice},
    thread::{AsThread, Thread},
    time::clocks::ProfClock,
    vm::vmar::Vmar,
};

mod init_proc;
mod job_control;
mod process_group;
mod session;
mod terminal;
pub(crate) mod timer_manager;

use atomic_integer_wrapper::define_atomic_version_of_integer_like_type;
pub use init_proc::spawn_init_process;
pub use job_control::JobControl;
use ostd::{
    sync::{RcuOption, RcuOptionReadGuard, WaitQueue},
    task::Task,
};
pub use process_group::ProcessGroup;
pub use session::Session;
pub use terminal::Terminal;

/// Process ID.
pub type Pid = u32;

/// The PID of the init process.
pub const INIT_PROCESS_PID: Pid = FIRST_POSIX_TID;

define_atomic_version_of_integer_like_type!(Pid, {
    /// Atomic [`Pid`].
    #[derive(Debug)]
    pub struct AtomicPid(AtomicU32);
});

/// Process group ID.
pub type Pgid = u32;
/// Session ID.
pub type Sid = u32;

/// The process group ID for process group created by [`ProcessGroup::new_bootstrap`].
const BOOTSTRAP_PGID: Pgid = 0;
/// The session ID for the session created by [`Session::new_bootstrap_pair`].
const BOOTSTRAP_SID: Sid = 0;

pub type ExitCode = u32;

pub(super) fn init_on_each_cpu() {
    timer_manager::init_on_each_cpu();
}

/// Process stands for a set of threads that shares the same userspace.
pub struct Process {
    // Immutable Part
    pid: Pid,
    /// The PID namespace this process belongs to.
    ///
    /// The process is also visible in all ancestor namespaces under the
    /// virtual PIDs recorded in `ns_vpids`.
    pid_ns: Arc<PidNamespace>,
    /// The process's virtual PID in its own PID namespace and in every
    /// ancestor namespace, innermost first. Immutable after creation.
    ns_vpids: Box<[(Arc<PidNamespace>, u32)]>,

    vmar: Mutex<Option<Arc<Vmar>>>,
    /// Wait for child status changed
    children_wait_queue: WaitQueue,
    pub(super) pidfile_pollee: Pollee,

    // Mutable Part
    /// The threads
    tasks: Mutex<TaskSet>,
    /// Process status
    status: ProcessStatus,
    /// Parent process
    pub(super) parent: ParentProcess,
    /// Children processes
    children: Mutex<Option<BTreeMap<Pid, Arc<Process>>>>,
    /// Process group
    pub(super) process_group: Mutex<Option<Arc<ProcessGroup>>>,
    /// The resource usage statistics of reaped child processes.
    reaped_children_stats: Mutex<ReapedChildrenStats>,
    /// resource limits
    resource_limits: ResourceLimits,
    /// The bound cgroup of the process.
    ///
    /// If this field is `None`, the process is bound to the root cgroup.
    cgroup: RcuOption<Arc<CgroupNode>>,
    /// Scheduling priority nice value
    /// According to POSIX.1, the nice value is a per-process attribute,
    /// the threads in a process should share a nice value.
    nice: AtomicNice,
    /// The adjustment value of the out-of-memory (OOM) killer score.
    // FIXME: Support OOM killer.
    oom_score_adj: AtomicI16,

    // Child reaper attribute
    /// Whether the process is a child subreaper.
    ///
    /// A subreaper can be considered as a sort of "sub-init".
    /// Instead of letting the init process to reap all orphan zombie processes,
    /// a subreaper can reap orphan zombie processes among its descendants.
    is_child_subreaper: AtomicBool,
    /// Whether the process has a subreaper that will reap it when the
    /// process becomes orphaned.
    ///
    /// If `has_child_subreaper` is true in a `Process`, this attribute should
    /// also be true for all of its descendants.
    pub(super) has_child_subreaper: AtomicBool,

    // Signal
    /// Sig dispositions
    sig_dispositions: Mutex<Arc<Mutex<SigDispositions>>>,
    /// The process-level sigqueue.
    sig_queues: SigQueues,
    signal_job_control: Mutex<SignalJobControl>,
    /// The signal that the process should receive when parent process exits.
    parent_death_signal: AtomicSigNum,
    /// The signal that should be sent to the parent when this process exits.
    exit_signal: AtomicSigNum,

    // Time
    /// A profiling clock measures the user CPU time and kernel CPU time of the current process.
    prof_clock: Arc<ProfClock>,
    /// A manager that manages timer resources and utilities of the process.
    timer_manager: PosixTimerManager,
    /// Process start time since boot.
    start_time: Duration,

    // Namespaces
    /// The user namespace
    user_ns: Mutex<Arc<UserNamespace>>,
}

impl Drop for Process {
    fn drop(&mut self) {
        self.pidfile_pollee.notify(IoEvents::HUP);
    }
}

/// Representing a parent process by holding a weak reference to it and its PID.
///
/// This type caches the value of the PID so that it can be retrieved cheaply.
///
/// The benefit of using `ParentProcess` over `(Mutex<Weak<Process>>, AtomicPid,)` is to
/// enforce the invariant that the cached PID and the weak reference are always kept in sync.
pub struct ParentProcess {
    process: Mutex<Weak<Process>>,
    pid: AtomicPid,
}

impl ParentProcess {
    pub fn new(process: Weak<Process>) -> Self {
        let pid = match process.upgrade() {
            Some(process) => process.pid(),
            None => 0,
        };

        Self {
            process: Mutex::new(process),
            pid: AtomicPid::new(pid),
        }
    }

    pub fn pid(&self) -> Pid {
        self.pid.load(Ordering::Relaxed)
    }

    pub fn lock(&self) -> ParentProcessGuard<'_> {
        ParentProcessGuard {
            guard: self.process.lock(),
            this: self,
        }
    }
}

pub struct ParentProcessGuard<'a> {
    guard: MutexGuard<'a, Weak<Process>>,
    this: &'a ParentProcess,
}

impl ParentProcessGuard<'_> {
    pub fn process(&self) -> &Weak<Process> {
        &self.guard
    }

    /// Update both pid and weak ref.
    pub fn set_process(&mut self, new_process: &Arc<Process>) {
        self.this.pid.store(new_process.pid(), Ordering::Relaxed);
        *self.guard = Arc::downgrade(new_process);
    }
}

impl Process {
    /// Returns the current process.
    ///
    /// It returns `None` if:
    ///  - the function is called in the bootstrap context;
    ///  - or if the current task is not associated with a process.
    pub fn current() -> Option<Arc<Process>> {
        Some(Task::current()?.as_posix_thread()?.process())
    }

    pub(super) fn new(
        pid: Pid,
        vmar: Arc<Vmar>,

        resource_limits: ResourceLimits,
        nice: Nice,
        oom_score_adj: i16,
        sig_dispositions: Arc<Mutex<SigDispositions>>,
        user_ns: Arc<UserNamespace>,
        pid_ns: Arc<PidNamespace>,
        pid_ns_reservation: PidNsReservation,
    ) -> Arc<Self> {
        let mut pid_ns_reservation = Some(pid_ns_reservation);
        Arc::new_cyclic(|process_ref: &Weak<Process>| {
            let ns_vpids = pid_ns_reservation.take().unwrap().commit(process_ref);

            // SIGCHID does not interrupt pauser. Child process will
            // resume paused parent when doing exit.
            let children_wait_queue = WaitQueue::new();

            let prof_clock = ProfClock::new();
            let timer_manager = PosixTimerManager::new(&prof_clock, process_ref);

            Self {
                pid,
                pid_ns,
                ns_vpids,
                vmar: Mutex::new(Some(vmar)),
                children_wait_queue,
                pidfile_pollee: Pollee::new(),
                tasks: Mutex::new(TaskSet::new()),
                status: ProcessStatus::default(),
                parent: ParentProcess::new(Weak::new()),
                children: Mutex::new(Some(BTreeMap::new())),
                process_group: Mutex::new(None),
                reaped_children_stats: Mutex::new(ReapedChildrenStats::default()),
                resource_limits,
                cgroup: RcuOption::new(None),
                nice: AtomicNice::new(nice),
                oom_score_adj: AtomicI16::new(oom_score_adj),
                is_child_subreaper: AtomicBool::new(false),
                has_child_subreaper: AtomicBool::new(false),
                sig_dispositions: Mutex::new(sig_dispositions),
                sig_queues: SigQueues::new(),
                signal_job_control: Mutex::new(SignalJobControl::default()),
                parent_death_signal: AtomicSigNum::new_empty(),
                exit_signal: AtomicSigNum::new_empty(),
                prof_clock,
                timer_manager,
                // Match /proc/uptime rather than accumulated timer IRQs,
                // which can lag behind the clock source during startup.
                start_time: aster_time::read_monotonic_time(),
                user_ns: Mutex::new(user_ns),
            }
        })
    }

    /// Runs the process.
    pub(super) fn run(&self) {
        let tasks = self.tasks.lock();
        // when run the process, the process should has only one thread
        debug_assert!(tasks.as_slice().len() == 1);
        debug_assert!(!self.status().is_zombie());
        let task = tasks.main().clone();
        // should not hold the lock when run thread
        drop(tasks);
        let thread = task.as_thread().unwrap();
        thread.run();
    }

    // *********** Basic structures ***********

    pub fn pid(&self) -> Pid {
        self.pid
    }

    /// Returns the PID namespace this process belongs to.
    pub fn pid_ns(&self) -> &Arc<PidNamespace> {
        &self.pid_ns
    }

    /// Returns the process's virtual PID in the given namespace, or `None`
    /// if the process is not visible in that namespace.
    pub fn pid_in_ns(&self, ns: &Arc<PidNamespace>) -> Option<u32> {
        self.ns_vpids
            .iter()
            .find(|(entry_ns, _)| Arc::ptr_eq(entry_ns, ns))
            .map(|(_, vpid)| *vpid)
    }

    /// Returns the process's virtual PID in its own PID namespace and in
    /// every ancestor namespace, innermost first.
    pub fn ns_vpids(&self) -> &[(Arc<PidNamespace>, u32)] {
        &self.ns_vpids
    }

    /// Returns whether this process is the init process of a non-initial
    /// PID namespace (i.e., virtual PID 1 in its own namespace).
    pub fn is_ns_init(&self) -> bool {
        !self.pid_ns.is_init() && self.pid_in_ns(&self.pid_ns.clone()) == Some(1)
    }

    /// Removes the process's virtual PID registrations from all PID
    /// namespaces it was visible in. Called when the process is reaped.
    pub(super) fn remove_from_pid_namespaces(&self) {
        for (ns, vpid) in self.ns_vpids.iter() {
            ns.remove_vpid(*vpid);
        }
    }

    /// Gets the profiling clock of the process.
    pub fn prof_clock(&self) -> &Arc<ProfClock> {
        &self.prof_clock
    }

    /// Gets the timer resources and utilities of the process.
    pub fn timer_manager(&self) -> &PosixTimerManager {
        &self.timer_manager
    }

    /// Returns the process start time since boot.
    pub fn start_time(&self) -> Duration {
        self.start_time
    }

    pub fn tasks(&self) -> &Mutex<TaskSet> {
        &self.tasks
    }

    pub fn resource_limits(&self) -> &ResourceLimits {
        &self.resource_limits
    }

    pub fn nice(&self) -> &AtomicNice {
        &self.nice
    }

    pub fn main_thread(&self) -> Arc<Thread> {
        self.tasks.lock().main().as_thread().unwrap().clone()
    }

    pub fn oom_score_adj(&self) -> &AtomicI16 {
        &self.oom_score_adj
    }

    // *********** Parent and child ***********

    pub fn parent(&self) -> &ParentProcess {
        &self.parent
    }

    pub fn is_init_process(&self) -> bool {
        self.parent.pid() == 0
    }

    pub(super) fn children(&self) -> &Mutex<Option<BTreeMap<Pid, Arc<Process>>>> {
        &self.children
    }

    pub fn children_wait_queue(&self) -> &WaitQueue {
        &self.children_wait_queue
    }

    pub fn reaped_children_stats(&self) -> &Mutex<ReapedChildrenStats> {
        &self.reaped_children_stats
    }

    // *********** Process group & Session ***********

    /// Returns the process group ID of the process.
    //
    // FIXME: If we call this method on a non-current process without holding the PID table
    // lock, it may return zero if the process is reaped at the same time.
    pub fn pgid(&self) -> Pgid {
        self.process_group
            .lock()
            .as_ref()
            .map_or(0, |group| group.pgid())
    }

    /// Returns the session ID of the process.
    //
    // FIXME: If we call this method on a non-current process without holding the PID table
    // lock, it may return zero if the process is reaped at the same time.
    pub fn sid(&self) -> Sid {
        self.process_group
            .lock()
            .as_ref()
            .map_or(0, |group| group.session().sid())
    }

    /// Returns the controlling terminal of the process, if any.
    pub fn terminal(&self) -> Option<Arc<dyn Terminal>> {
        self.process_group
            .lock()
            .as_ref()
            .and_then(|group| group.session().lock().terminal().cloned())
    }

    /// Moves the process to the new session.
    ///
    /// This method will create a new process group in a new session, move the process to the new
    /// session, and return the session ID (which is equal to the process ID and the process group
    /// ID).
    ///
    /// # Errors
    ///
    /// This method will return `EPERM` if an existing process group has the same identifier as the
    /// process ID. This means that the process is or was a process group leader and that the
    /// process group is still alive.
    pub fn to_new_session(self: &Arc<Self>) -> Result<Sid> {
        // Lock order: PID table -> group of process -> group inner -> session inner
        let mut pid_table = pid_table::pid_table_mut();

        if pid_table.contains_process_group(&self.pid) {
            return_errno_with_message!(
                Errno::EPERM,
                "a process group leader cannot be moved to a new session"
            );
        }

        let mut process_group_mut = self.process_group.lock();

        self.clear_old_group_and_session(&mut process_group_mut, &mut pid_table);

        Ok(self.set_new_session(&mut process_group_mut, &mut pid_table))
    }

    pub(super) fn clear_old_group_and_session(
        &self,
        process_group_mut: &mut MutexGuard<Option<Arc<ProcessGroup>>>,
        pid_table: &mut PidTable,
    ) {
        let process_group = process_group_mut.take().unwrap();
        let mut process_group_inner = process_group.lock();
        let session = process_group.session();
        let mut session_inner = session.lock();

        // Remove the process from the process group.
        process_group_inner.remove_process(&self.pid);
        if process_group_inner.is_empty() {
            pid_table.remove_process_group(process_group.pgid());

            // Remove the process group from the session.
            session_inner.remove_process_group(&process_group.pgid());
            if session_inner.is_empty() {
                pid_table.remove_session(session.sid());
            }
        }
    }

    fn set_new_session(
        self: &Arc<Self>,
        process_group_mut: &mut MutexGuard<Option<Arc<ProcessGroup>>>,
        pid_table: &mut PidTable,
    ) -> Sid {
        let (session, process_group) = Session::new_pair(self);
        let sid = session.sid();

        // Insert the new session and the new process group to the global table.
        pid_table.insert_session(session.sid(), &session);
        pid_table.insert_process_group(process_group.pgid(), &process_group);
        **process_group_mut = Some(process_group);

        sid
    }

    /// Moves the process itself or its child process to another process group.
    ///
    /// The process to be moved is specified with the process ID `pid`; `self` is used only for
    /// permission checking purposes (see the Errors section below), which is typically
    /// `current!()` when implementing system calls.
    ///
    /// If `pgid` is equal to the process ID, a new process group with the given PGID will be
    /// created (if it does not already exist). Then, the process will be moved to the process
    /// group with the given PGID, if the process group exists and belongs to the same session as
    /// the given process.
    ///
    /// # Errors
    ///
    /// This method will return `ESRCH` in following cases:
    ///  * The process specified by `pid` does not exist;
    ///  * The process specified by `pid` is neither `self` or a child process of `self`.
    ///
    /// This method will return `EPERM` in following cases:
    ///  * The process is not in the same session as `self`;
    ///  * The process is a session leader, but the given PGID is not the process's PID/PGID;
    ///  * The process group already exists, but the group does not belong to the same session;
    ///  * The process group does not exist, but `pgid` is not equal to the process ID.
    pub fn move_process_to_group(&self, pid: Pid, pgid: Pgid) -> Result<()> {
        // Lock order: PID table -> group of process -> group inner -> session inner
        let mut pid_table = pid_table::pid_table_mut();

        let process = pid_table.get_process(pid).ok_or_else(|| {
            Error::with_message(Errno::ESRCH, "the process to set the PGID does not exist")
        })?;

        let current_session = if self.pid == process.pid() {
            // There is no need to check if the session is the same in this case.
            None
        } else if self.pid == process.parent().pid() {
            // FIXME: If the child process has called `execve`, we should fail with `EACCESS`.

            // Immediately release the `self.process_group` lock to avoid deadlocks. Race
            // conditions don't matter because this is used for comparison purposes only.
            Some(
                self.process_group
                    .lock()
                    .as_ref()
                    .unwrap()
                    .session()
                    .clone(),
            )
        } else {
            return_errno_with_message!(
                Errno::ESRCH,
                "the process to set the PGID is neither the current process nor its child process"
            );
        };

        if let Some(new_process_group) = pid_table.get_process_group(&pgid) {
            process.to_existing_group(current_session, &mut pid_table, new_process_group)
        } else if pgid == process.pid() {
            process.to_new_group(current_session, &mut pid_table)
        } else {
            return_errno_with_message!(Errno::EPERM, "the new process group does not exist");
        }
    }

    /// Moves the process to an existing group.
    fn to_existing_group(
        self: &Arc<Self>,
        current_session: Option<Arc<Session>>,
        pid_table: &mut PidTable,
        new_process_group: Arc<ProcessGroup>,
    ) -> Result<()> {
        let mut process_group_mut = self.process_group.lock();

        let process_group = process_group_mut.as_ref().unwrap();
        let session = process_group.session();

        if session.sid() == self.pid {
            return_errno_with_message!(
                Errno::EPERM,
                "a session leader cannot be moved to a new process group"
            );
        }
        if !Arc::ptr_eq(session, new_process_group.session()) {
            return_errno_with_message!(
                Errno::EPERM,
                "the new process group does not belong to the same session"
            );
        }
        if current_session
            .as_ref()
            .is_some_and(|current| !Arc::ptr_eq(current, session))
        {
            return_errno_with_message!(Errno::EPERM, "the process belongs to a different session");
        }

        // Lock order: group with a smaller PGID -> group with a larger PGID
        let (mut process_group_inner, mut new_group_inner) =
            match process_group.pgid().cmp(&new_process_group.pgid()) {
                core::cmp::Ordering::Less => {
                    let process_group_inner = process_group.lock();
                    let new_group_inner = new_process_group.lock();
                    (process_group_inner, new_group_inner)
                }
                core::cmp::Ordering::Greater => {
                    let new_group_inner = new_process_group.lock();
                    let process_group_inner = process_group.lock();
                    (process_group_inner, new_group_inner)
                }
                core::cmp::Ordering::Equal => return Ok(()),
            };

        // Remove the process from the old process group
        let mut session_inner = session.lock();
        process_group_inner.remove_process(&self.pid);
        if process_group_inner.is_empty() {
            pid_table.remove_process_group(process_group.pgid());
            session_inner.remove_process_group(&process_group.pgid());
        }
        drop(session_inner);
        drop(process_group_inner);

        // Insert the process to the new process group
        new_group_inner.insert_process(self);
        drop(new_group_inner);
        *process_group_mut = Some(new_process_group);

        Ok(())
    }

    /// Creates a new process group and moves the process to the group.
    fn to_new_group(
        self: &Arc<Self>,
        current_session: Option<Arc<Session>>,
        pid_table: &mut PidTable,
    ) -> Result<()> {
        let mut process_group_mut = self.process_group.lock();

        let process_group = process_group_mut.as_ref().unwrap();
        let session = process_group.session();

        if current_session
            .as_ref()
            .is_some_and(|current| !Arc::ptr_eq(current, session))
        {
            return_errno_with_message!(Errno::EPERM, "the process belongs to a different session");
        }
        if process_group.pgid() == self.pid {
            // We'll hit this if the process is a session leader. There is no need to check below.
            return Ok(());
        }

        // Remove the process from the old process group
        let mut process_group_inner = process_group.lock();
        let mut session_inner = session.lock();
        process_group_inner.remove_process(&self.pid);
        if process_group_inner.is_empty() {
            pid_table.remove_process_group(process_group.pgid());
            session_inner.remove_process_group(&process_group.pgid());
        }
        drop(session_inner);
        drop(process_group_inner);

        // Create a new process group and insert the process to it
        let new_process_group = ProcessGroup::new(self, session.clone());
        pid_table.insert_process_group(new_process_group.pgid(), &new_process_group);
        session.lock().insert_process_group(&new_process_group);
        *process_group_mut = Some(new_process_group);

        Ok(())
    }

    // ************** Virtual Memory *************

    pub fn lock_vmar(&self) -> ProcessVmarGuard<'_> {
        ProcessVmarGuard::new(self.vmar.lock())
    }

    // ****************** Signal ******************

    pub fn sig_dispositions(&self) -> &Mutex<Arc<Mutex<SigDispositions>>> {
        &self.sig_dispositions
    }

    pub(super) fn sig_queues(&self) -> &SigQueues {
        &self.sig_queues
    }

    pub(super) fn signal_job_control(&self) -> &Mutex<SignalJobControl> {
        &self.signal_job_control
    }

    /// Enqueues a process-directed signal.
    ///
    /// This method does not perform permission checks on user signals.
    /// Therefore, unless the caller can ensure that there are no permission issues,
    /// this method should be used to enqueue kernel signals or fault signals.
    pub fn enqueue_signal(&self, signal: Box<dyn Signal>) {
        self.enqueue_signal_for_thread(signal, None);
    }

    /// Common generation path; `None` selects the process-wide pending queue.
    pub(super) fn enqueue_signal_for_thread(
        &self,
        signal: Box<dyn Signal>,
        target: Option<&PosixThread>,
    ) {
        if self.status.is_zombie() {
            return;
        }

        let is_sigcont = signal.num() == SIGCONT;
        let queue = target.map_or(&self.sig_queues, PosixThread::sig_queues);
        let (enqueued, resumed) = if is_sigcont || is_stop_signal(signal.num()) {
            // Stabilize membership before taking the coordinator: newly created
            // threads cannot miss a process-wide cancellation.
            let tasks = self.tasks.lock();
            let mut control = self.signal_job_control.lock();
            let discarded = if is_sigcont {
                SigSet::from(SIGSTOP) | SIGTSTP | SIGTTIN | SIGTTOU
            } else {
                SigSet::from(SIGCONT)
            };
            self.sig_queues.discard(discarded);
            for task in tasks.as_slice() {
                let thread = task.as_posix_thread().unwrap();
                thread.sig_queues().discard(discarded);
                if is_sigcont {
                    control.cancel(&mut thread.selected_stop().lock());
                    *thread.group_stop_participant().lock() = Default::default();
                }
            }
            // Generation effects apply even when blocked, ignored, or coalesced.
            let resumed = is_sigcont && control.resume(&self.status);
            (queue.enqueue_without_notify(signal), resumed)
        } else {
            // In particular SIGKILL must not reacquire task membership: exit and
            // exec already hold it when terminating sibling threads.
            let _control = self.signal_job_control.lock();
            (queue.enqueue_without_notify(signal), false)
        };

        // Release the coordinator and the task-set guard acquired here before
        // observer callbacks. Sibling SIGKILL callers may already hold task membership.
        if enqueued {
            queue.notify_enqueue();
        }
        if resumed && let Some(parent) = self.parent.lock().process().upgrade() {
            parent.children_wait_queue.wake_all();
        }
        if let Some(target) = target
            && !is_sigcont
        {
            target.wake_signalled_waker();
        } else {
            // FIXME: Process-directed signals currently wake all threads instead
            // of selecting one eligible recipient as Linux does.
            let tasks = self.tasks.lock().as_slice().to_vec();
            for task in tasks {
                task.as_posix_thread().unwrap().wake_signalled_waker();
            }
        }
    }

    /// Clears the parent death signal.
    pub fn clear_parent_death_signal(&self) {
        self.parent_death_signal.clear();
    }

    /// Sets the parent death signal as `signum`.
    pub fn set_parent_death_signal(&self, sig_num: SigNum) {
        self.parent_death_signal.set(sig_num);
    }

    /// Returns the parent death signal.
    ///
    /// The parent death signal is the signal will be sent to child processes
    /// when the process exits.
    pub fn parent_death_signal(&self) -> Option<SigNum> {
        self.parent_death_signal.as_sig_num()
    }

    pub fn set_exit_signal(&self, sig_num: SigNum) {
        self.exit_signal.set(sig_num);
    }

    pub fn exit_signal(&self) -> Option<SigNum> {
        self.exit_signal.as_sig_num()
    }

    // ******************* Status ********************

    /// Returns a reference to the process status.
    pub fn status(&self) -> &ProcessStatus {
        &self.status
    }

    /// Commits a selected stop only if no CONT or exit has superseded it.
    pub(super) fn stop_if_selected(&self, thread: &PosixThread, sig_num: SigNum) {
        let tasks = {
            let tasks = self.tasks.lock();
            let mut control = self.signal_job_control.lock();
            // A signal-delivery ptrace stop may have overlapped a sibling's
            // group-stop initiation. Participate in that CURRENT obligation;
            // do not let the tracer's injected signal replace its stop round.
            if thread.group_stop_participant().lock().must_stop() {
                control.cancel(&mut thread.selected_stop().lock());
                return;
            }
            let kill_pending = thread.sig_queues().has_pending_signal(SIGKILL)
                || self.sig_queues.has_pending_signal(SIGKILL);
            if !control.begin_stop(
                &mut thread.selected_stop().lock(),
                &self.status,
                sig_num,
                kill_pending || tasks.in_execve(),
            ) {
                return;
            }
            for task in tasks.as_slice() {
                if !task.as_thread().unwrap().is_exited() {
                    control.enroll(
                        &mut task
                            .as_posix_thread()
                            .unwrap()
                            .group_stop_participant()
                            .lock(),
                    );
                }
            }
            tasks.as_slice().to_vec()
        };
        for task in tasks {
            task.as_posix_thread().unwrap().wake_signalled_waker();
        }
    }

    /// Returns whether this thread must acknowledge the current stop episode.
    pub(crate) fn has_group_stop_checkpoint(&self, thread: &PosixThread) -> bool {
        let _control = self.signal_job_control.lock();
        matches!(
            *thread.group_stop_participant().lock(),
            GroupStopParticipant::Pending { .. }
        )
    }

    /// Enrolls a new member while the caller still owns task membership.
    pub(super) fn enroll_group_stop(&self, thread: &PosixThread) {
        self.signal_job_control
            .lock()
            .enroll(&mut thread.group_stop_participant().lock());
    }

    /// Returns whether the process is stopped.
    pub fn is_stopped(&self) -> bool {
        self.status.stop_status().is_stopped()
    }

    /// Returns whether returning members have group-stop work to handle.
    pub(crate) fn has_group_stop_work(&self, thread: &PosixThread) -> bool {
        self.thread_must_stop(thread) || self.status.stop_status().notification_pending()
    }

    /// Checks the thread's obligation, not the process completion latch: a
    /// tracer can explicitly resume one member while its siblings stay stopped.
    pub(crate) fn thread_must_stop(&self, thread: &PosixThread) -> bool {
        if !self.is_stopped() {
            return false;
        }
        let _control = self.signal_job_control.lock();
        thread.group_stop_participant().lock().must_stop()
    }

    /// Delivers a claimed group-state notification with all child locks released.
    pub(super) fn notify_group_stop(&self) {
        if !self.status.stop_status().notification_pending() {
            return;
        }
        let Some(event) = self
            .signal_job_control
            .lock()
            .take_notification(&self.status)
        else {
            return;
        };
        self.notify_group_stop_event(event, None);
    }

    /// Delivers a previously claimed event, optionally omitting the tracer's
    /// process when its per-thread stop notification already covers this event.
    pub(super) fn notify_group_stop_event(&self, event: StopWaitStatus, tracer: Option<&Process>) {
        let Some(parent) = self.parent.lock().process().upgrade() else {
            return;
        };
        if tracer.is_some_and(|tracer| core::ptr::eq(parent.as_ref(), tracer)) {
            return;
        }
        let (code, status) = match event {
            StopWaitStatus::Stopped(signum) => (CLD_STOPPED, signum.as_u8() as i32),
            StopWaitStatus::Continue => (CLD_CONTINUED, SIGCONT.as_u8() as i32),
        };
        let info = self.child_state_siginfo(&parent, code, status);
        parent.notify_child_state(info);
    }

    /// Publishes a child-state signal and wakes waiters, honoring this parent's
    /// dispositions for both ordinary children and ptrace notifications.
    pub(super) fn notify_child_state(&self, info: siginfo_t) {
        // Serialize disposition checking with enqueue, but release those locks
        // before queue observers and wake callbacks. SA_NOCLDSTOP suppresses
        // SIGCHLD only: waiters must still observe the committed wait status.
        let enqueued = {
            let dispositions = self.sig_dispositions.lock();
            let dispositions = dispositions.lock();
            let action = dispositions.get(SIGCHLD);
            let suppressed = action.handler() == SigHandler::Ign
                || action.flags().contains(SigActionFlags::SA_NOCLDSTOP);
            if suppressed {
                false
            } else {
                let _control = self.signal_job_control.lock();
                self.sig_queues
                    .enqueue_without_notify(Box::new(RawSignal::new(info)))
            }
        };
        if enqueued {
            self.sig_queues.notify_enqueue();
            let tasks = self.tasks.lock().as_slice().to_vec();
            for task in tasks {
                task.as_posix_thread().unwrap().wake_signalled_waker();
            }
        }
        self.children_wait_queue.wake_all();
    }

    /// Builds a child-state payload in the parent's PID and user namespaces.
    pub(super) fn child_state_siginfo(
        &self,
        parent: &Process,
        code: i32,
        status: i32,
    ) -> siginfo_t {
        let mut info = siginfo_t::new(SIGCHLD, code);
        let main_thread = self.main_thread();
        let uid = main_thread.as_posix_thread().unwrap().credentials().ruid();
        info.set_pid_uid(
            self.pid_in_ns(parent.pid_ns()).unwrap_or(0),
            parent.user_ns().lock().map_kuid(uid),
        );
        info.set_status(status);
        info
    }

    /// Gets and clears the stop status changes for the `wait` syscall.
    pub(super) fn wait_stopped_or_continued(&self, options: WaitOptions) -> Option<StopWaitStatus> {
        self.status.stop_status().wait(options)
    }

    // ******************* Subreaper ********************

    /// Sets the child subreaper attribute of the current process.
    ///
    /// # Panics
    ///
    /// This method may panic if the process is a zombie process.
    pub fn set_child_subreaper(&self) {
        self.is_child_subreaper.store(true, Ordering::Relaxed);
        let has_child_subreaper = self.has_child_subreaper.fetch_or(true, Ordering::Release);
        if !has_child_subreaper {
            self.propagate_has_child_subreaper();
        }
    }

    /// Unsets the child subreaper attribute of the current process.
    pub fn unset_child_subreaper(&self) {
        self.is_child_subreaper.store(false, Ordering::Relaxed);
    }

    /// Returns whether this process is a child subreaper.
    pub fn is_child_subreaper(&self) -> bool {
        self.is_child_subreaper.load(Ordering::Relaxed)
    }

    /// Sets all descendants of the current process as having child subreaper.
    fn propagate_has_child_subreaper(&self) {
        let mut process_queue = VecDeque::new();
        let children = self.children().lock();
        for child_process in children.as_ref().unwrap().values() {
            let has_child_subreaper = child_process
                .has_child_subreaper
                .fetch_or(true, Ordering::Release);
            if !has_child_subreaper {
                process_queue.push_back(child_process.clone());
            }
        }

        while let Some(process) = process_queue.pop_front() {
            let children = process.children().lock();
            let Some(children_ref) = children.as_ref() else {
                // The process is exiting group at the same time.
                continue;
            };
            for child_process in children_ref.values() {
                let has_child_subreaper = child_process
                    .has_child_subreaper
                    .fetch_or(true, Ordering::Release);
                if !has_child_subreaper {
                    process_queue.push_back(child_process.clone());
                }
            }
        }
    }

    pub fn user_ns(&self) -> &Mutex<Arc<UserNamespace>> {
        &self.user_ns
    }

    // ******************* cgroup ********************

    /// Returns a RCU read guard to the cgroup of the process.
    ///
    /// The returned cgroup is not a stable snapshot. It may be changed by other threads
    /// and encounter race conditions. Users can use [`CgroupMembership`] to obtain
    /// a lock to prevent the cgroup from being changed.
    ///
    /// [`CgroupMembership`]: crate::fs::cgroupfs::CgroupMembership
    pub fn cgroup(&self) -> RcuOptionReadGuard<'_, Arc<CgroupNode>> {
        self.cgroup.read()
    }

    /// Sets the cgroup for this process.
    ///
    /// Note: This function should only be called within the cgroup module.
    /// Arbitrary calls may likely cause race conditions.
    #[doc(hidden)]
    pub fn set_cgroup(&self, cgroup: Option<Arc<CgroupNode>>) {
        self.cgroup.update(cgroup);
    }
}

/// Enqueues a process-directed kernel signal asynchronously.
///
/// This is the asynchronous version of [`Process::enqueue_signal`]. By asynchronous, this method
/// submits a work item and returns, so this method doesn't sleep and can be used in atomic mode.
pub fn enqueue_signal_async(process: Weak<Process>, signum: SigNum) {
    use super::signal::signals::kernel::KernelSignal;
    use crate::thread::work_queue;

    work_queue::submit_work_func(
        move || {
            if let Some(process) = process.upgrade() {
                process.enqueue_signal(Box::new(KernelSignal::new(signum)));
            }
        },
        work_queue::WorkPriority::High,
    );
}

/// Broadcasts a process-directed kernel signal asynchronously.
///
/// This is the asynchronous version of [`ProcessGroup::broadcast_signal`]. By asynchronous, this
/// method submits a work item and returns, so this method doesn't sleep and can be used in atomic
/// mode.
pub fn broadcast_signal_async(process_group: Weak<ProcessGroup>, signum: SigNum) {
    use super::signal::signals::kernel::KernelSignal;
    use crate::thread::work_queue;

    work_queue::submit_work_func(
        move || {
            if let Some(process_group) = process_group.upgrade() {
                process_group.broadcast_signal(KernelSignal::new(signum));
            }
        },
        work_queue::WorkPriority::High,
    );
}

/// The resource usage statistics of child processes that have terminated and
/// been reaped by the parent.
///
/// These statistics include the resources consumed by grandchildren and more
/// distant descendants, if all intermediate child processes have waited on
/// their own terminated children.
#[derive(Default)]
pub struct ReapedChildrenStats {
    user_time: Duration,
    kernel_time: Duration,
}

impl ReapedChildrenStats {
    pub fn add(&mut self, utime: Duration, stime: Duration) {
        self.user_time += utime;
        self.kernel_time += stime;
    }

    pub fn get(&self) -> (Duration, Duration) {
        (self.user_time, self.kernel_time)
    }
}
