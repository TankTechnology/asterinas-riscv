// SPDX-License-Identifier: MPL-2.0

//! System call handlers.

#![cfg_attr(
    any(
        target_arch = "x86_64",
        target_arch = "riscv64",
        target_arch = "loongarch64"
    ),
    expect(dead_code)
)]

use core::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};

pub use clock_gettime::ClockId;
use ostd::{arch::cpu::context::UserContext, timer::Jiffies, user::UserContextApi};
pub use timer_create::create_timer;

use crate::{cpu::LinuxAbi, prelude::*};

const SYSCALL_PROFILE_SLOTS: usize = 21;
const SYSCALL_PROFILE_LOG_INTERVAL: u64 = 16_384;
const SYSCALL_PROFILE_SLOW_THRESHOLD_JIFFIES: u64 = 64;
const SYSCALL_PROFILE_SLOW_PID_SLOTS: usize = 256;
const SYSCALL_PROFILE_SLOW_LOG_LIMIT_PER_PID: u64 = 64;
const SYSCALL_PROFILE_PID_LOG_INTERVAL: u64 = 512;

// Keep syscall profiling opt-in; diagnostic runs enable it with the early
// command-line parameter without imposing work on normal Firefox startup.
static SYSCALL_PROFILE: AtomicBool = AtomicBool::new(false);
static SYSCALL_PROFILE_EVENTS: AtomicU64 = AtomicU64::new(0);
static SYSCALL_PROFILE_ENTERED: [AtomicU64; SYSCALL_PROFILE_SLOTS] =
    [const { AtomicU64::new(0) }; SYSCALL_PROFILE_SLOTS];
static SYSCALL_PROFILE_COMPLETED: [AtomicU64; SYSCALL_PROFILE_SLOTS] =
    [const { AtomicU64::new(0) }; SYSCALL_PROFILE_SLOTS];
static SYSCALL_PROFILE_JIFFIES: [AtomicU64; SYSCALL_PROFILE_SLOTS] =
    [const { AtomicU64::new(0) }; SYSCALL_PROFILE_SLOTS];
static SYSCALL_PROFILE_FORK_EXEC_EVENTS: AtomicU64 = AtomicU64::new(0);
static SYSCALL_PROFILE_SLOW_PID_KEYS: [AtomicU32; SYSCALL_PROFILE_SLOW_PID_SLOTS] =
    [const { AtomicU32::new(0) }; SYSCALL_PROFILE_SLOW_PID_SLOTS];
static SYSCALL_PROFILE_SLOW_PID_EVENTS: [AtomicU64; SYSCALL_PROFILE_SLOW_PID_SLOTS] =
    [const { AtomicU64::new(0) }; SYSCALL_PROFILE_SLOW_PID_SLOTS];
static SYSCALL_PROFILE_PID_KEYS: [AtomicU32; SYSCALL_PROFILE_SLOW_PID_SLOTS] =
    [const { AtomicU32::new(0) }; SYSCALL_PROFILE_SLOW_PID_SLOTS];
static SYSCALL_PROFILE_PID_EVENTS: [AtomicU64; SYSCALL_PROFILE_SLOW_PID_SLOTS] =
    [const { AtomicU64::new(0) }; SYSCALL_PROFILE_SLOW_PID_SLOTS];
static SYSCALL_PROFILE_PID_COUNTS: [AtomicU64;
    SYSCALL_PROFILE_SLOW_PID_SLOTS * SYSCALL_PROFILE_SLOTS] =
    [const { AtomicU64::new(0) }; SYSCALL_PROFILE_SLOW_PID_SLOTS * SYSCALL_PROFILE_SLOTS];
static SYSCALL_PROFILE_PID_JIFFIES: [AtomicU64;
    SYSCALL_PROFILE_SLOW_PID_SLOTS * SYSCALL_PROFILE_SLOTS] =
    [const { AtomicU64::new(0) }; SYSCALL_PROFILE_SLOW_PID_SLOTS * SYSCALL_PROFILE_SLOTS];

// Parse this switch during the early command-line pass so the first userspace
// syscall is observable even if later boot setup is slow.
aster_cmdline::define_flag_param_early!("asterinas.syscall_profile", SYSCALL_PROFILE);

/// Return the compact profiling slot for the syscall numbers that dominate the
/// Firefox startup trace.  The profiler is deliberately opt-in and only tracks
/// these calls so that normal syscall dispatch remains unchanged.
fn syscall_profile_slot(number: u64) -> Option<usize> {
    match number {
        57 => Some(0),   // close
        73 => Some(1),   // ppoll
        98 => Some(2),   // futex
        113 => Some(3),  // clock_gettime
        124 => Some(4),  // sched_yield
        220 => Some(5),  // clone
        221 => Some(6),  // execve
        63 => Some(7),   // read
        64 => Some(8),   // write
        56 => Some(9),   // openat
        222 => Some(10), // mmap
        215 => Some(11), // munmap
        226 => Some(12), // mprotect
        29 => Some(13),  // ioctl
        79 => Some(14),  // newfstatat
        80 => Some(15),  // fstat
        178 => Some(16), // gettid
        172 => Some(17), // getpid
        278 => Some(18), // getrandom
        135 => Some(19), // rt_sigprocmask
        22 => Some(20),  // epoll_pwait
        _ => None,
    }
}

fn syscall_profile_label(slot: usize) -> &'static str {
    match slot {
        0 => "close",
        1 => "ppoll",
        2 => "futex",
        3 => "clock_gettime",
        4 => "sched_yield",
        5 => "clone",
        6 => "execve",
        7 => "read",
        8 => "write",
        9 => "openat",
        10 => "mmap",
        11 => "munmap",
        12 => "mprotect",
        13 => "ioctl",
        14 => "newfstatat",
        15 => "fstat",
        16 => "gettid",
        17 => "getpid",
        18 => "getrandom",
        19 => "rt_sigprocmask",
        20 => "epoll_pwait",
        _ => "unknown",
    }
}

fn syscall_profile_pid_slot(pid: u32) -> Option<usize> {
    let slot = (pid as usize) % SYSCALL_PROFILE_SLOW_PID_SLOTS;
    let key = &SYSCALL_PROFILE_PID_KEYS[slot];
    let observed = key.load(Ordering::Relaxed);
    if observed == 0 {
        let _ = key.compare_exchange(0, pid, Ordering::Relaxed, Ordering::Relaxed);
    }
    (key.load(Ordering::Relaxed) == pid).then_some(slot)
}

fn syscall_profile_pid_snapshot(slot: usize, pid: u32, events: u64) {
    let mut snapshot = String::new();
    for syscall_slot in 0..SYSCALL_PROFILE_SLOTS {
        use core::fmt::Write;

        let base = slot * SYSCALL_PROFILE_SLOTS + syscall_slot;
        let _ = write!(
            snapshot,
            " {}={}/{}",
            syscall_profile_label(syscall_slot),
            SYSCALL_PROFILE_PID_COUNTS[base].load(Ordering::Relaxed),
            SYSCALL_PROFILE_PID_JIFFIES[base].load(Ordering::Relaxed),
        );
    }
    ostd::early_println!(
        "ASTERINAS_SYSCALL_PROFILE pid_snapshot pid={} events={}{}",
        pid,
        events,
        snapshot,
    );
}

fn syscall_profile_snapshot() -> String {
    let mut snapshot = String::new();
    for slot in 0..SYSCALL_PROFILE_SLOTS {
        use core::fmt::Write;

        let _ = write!(
            snapshot,
            " {}={}/{}/{}",
            syscall_profile_label(slot),
            SYSCALL_PROFILE_ENTERED[slot].load(Ordering::Relaxed),
            SYSCALL_PROFILE_COMPLETED[slot].load(Ordering::Relaxed),
            SYSCALL_PROFILE_JIFFIES[slot].load(Ordering::Relaxed),
        );
    }
    snapshot
}

fn syscall_profile_begin(number: u64, ctx: &Context) -> Option<(usize, u64, u32, u32, u64)> {
    if !SYSCALL_PROFILE.load(Ordering::Relaxed) {
        return None;
    }
    let event = SYSCALL_PROFILE_EVENTS.fetch_add(1, Ordering::Relaxed) + 1;
    let Some(slot) = syscall_profile_slot(number) else {
        if event == 1 || event.is_multiple_of(SYSCALL_PROFILE_LOG_INTERVAL) {
            ostd::early_println!(
                "ASTERINAS_SYSCALL_PROFILE events={} phase=unmatched syscall_number={}",
                event,
                number
            );
        }
        return None;
    };
    SYSCALL_PROFILE_ENTERED[slot].fetch_add(1, Ordering::Relaxed);
    if event == 1 || event.is_multiple_of(SYSCALL_PROFILE_LOG_INTERVAL) {
        ostd::early_println!(
            "ASTERINAS_SYSCALL_PROFILE events={} phase=enter{}",
            event,
            syscall_profile_snapshot()
        );
    }
    Some((
        slot,
        Jiffies::elapsed().as_u64(),
        ctx.process.pid(),
        ctx.posix_thread.tid(),
        number,
    ))
}

fn syscall_profile_end(start: Option<(usize, u64, u32, u32, u64)>) {
    let Some((slot, start, pid, tid, number)) = start else {
        return;
    };
    SYSCALL_PROFILE_COMPLETED[slot].fetch_add(1, Ordering::Relaxed);
    let elapsed = Jiffies::elapsed().as_u64().saturating_sub(start);
    SYSCALL_PROFILE_JIFFIES[slot].fetch_add(elapsed, Ordering::Relaxed);
    if let Some(pid_slot) = syscall_profile_pid_slot(pid) {
        let base = pid_slot * SYSCALL_PROFILE_SLOTS + slot;
        SYSCALL_PROFILE_PID_COUNTS[base].fetch_add(1, Ordering::Relaxed);
        SYSCALL_PROFILE_PID_JIFFIES[base].fetch_add(elapsed, Ordering::Relaxed);
        let events = SYSCALL_PROFILE_PID_EVENTS[pid_slot].fetch_add(1, Ordering::Relaxed) + 1;
        if events.is_multiple_of(SYSCALL_PROFILE_PID_LOG_INTERVAL) {
            syscall_profile_pid_snapshot(pid_slot, pid, events);
        }
    }
    if elapsed >= SYSCALL_PROFILE_SLOW_THRESHOLD_JIFFIES {
        // Keep a separate bounded stream for every process. A single global
        // cap would be exhausted by systemd's early epoll/clone activity
        // before Firefox is exec'd, hiding the very process being diagnosed.
        let pid_slot = (pid as usize) % SYSCALL_PROFILE_SLOW_PID_SLOTS;
        let key = &SYSCALL_PROFILE_SLOW_PID_KEYS[pid_slot];
        let observed = key.load(Ordering::Relaxed);
        if observed == 0 {
            let _ = key.compare_exchange(0, pid, Ordering::Relaxed, Ordering::Relaxed);
        }
        if key.load(Ordering::Relaxed) == pid {
            let event =
                SYSCALL_PROFILE_SLOW_PID_EVENTS[pid_slot].fetch_add(1, Ordering::Relaxed) + 1;
            if event <= SYSCALL_PROFILE_SLOW_LOG_LIMIT_PER_PID {
                ostd::early_println!(
                    "ASTERINAS_SYSCALL_PROFILE slow={} pid={} tid={} syscall={} name={} elapsed_jiffies={}",
                    event,
                    pid,
                    tid,
                    number,
                    syscall_profile_label(slot),
                    elapsed,
                );
            }
        }
    }
}

fn syscall_profile_log_process_boundary(number: u64, args: &[u64; 6], ctx: &Context) {
    if !SYSCALL_PROFILE.load(Ordering::Relaxed) || (number != 220 && number != 221) {
        return;
    }
    let event = SYSCALL_PROFILE_FORK_EXEC_EVENTS.fetch_add(1, Ordering::Relaxed) + 1;
    if event <= 128 || event.is_multiple_of(256) {
        ostd::early_println!(
            "ASTERINAS_SYSCALL_PROFILE boundary={} pid={} tid={} syscall={} arg0=0x{:x} arg1=0x{:x} arg2=0x{:x}",
            event,
            ctx.process.pid(),
            ctx.posix_thread.tid(),
            number,
            args[0],
            args[1],
            args[2],
        );
    }
}

#[cfg_attr(target_arch = "x86_64", path = "arch/x86.rs")]
#[cfg_attr(target_arch = "riscv64", path = "arch/riscv.rs")]
#[cfg_attr(target_arch = "loongarch64", path = "arch/loongarch.rs")]
mod arch;

mod accept;
mod access;
mod alarm;
#[cfg(target_arch = "x86_64")]
mod arch_prctl;
mod bind;
mod bpf;
mod brk;
mod capget;
mod capset;
mod chdir;
mod chmod;
mod chown;
mod chroot;
mod clock_gettime;
mod clock_settime;
mod clone;
mod close;
mod connect;
mod constants;
mod copy_file_range;
mod dup;
mod epoll;
mod eventfd;
mod execve;
mod exit;
mod exit_group;
mod fadvise64;
mod fallocate;
mod fanotify;
mod fcntl;
mod flock;
mod fork;
mod fsconfig;
mod fsmount;
mod fsopen;
mod fsync;
mod futex;
mod get_ioprio;
mod get_priority;
mod getcpu;
mod getcwd;
mod getdents64;
mod getegid;
mod geteuid;
mod getgid;
mod getgroups;
mod getpeername;
mod getpgid;
mod getpgrp;
mod getpid;
mod getppid;
mod getrandom;
mod getresgid;
mod getresuid;
mod getrusage;
mod getsid;
mod getsockname;
mod getsockopt;
mod gettid;
mod gettimeofday;
mod getuid;
mod getxattr;
mod inotify;
mod ioctl;
mod keyctl;
mod kill;
mod landlock;
mod link;
mod listen;
mod listmount;
mod listxattr;
mod lseek;
mod madvise;
mod membarrier;
mod memfd_create;
mod mkdir;
mod mknod;
mod mlock;
mod mmap;
mod mount;
mod mount_setattr;
mod move_mount;
mod mprotect;
mod mremap;
mod msync;
mod munmap;
mod name_to_handle_at;
mod nanosleep;
mod open;
mod openat2;
mod pause;
mod personality;
mod pidfd_getfd;
mod pidfd_open;
mod pidfd_send_signal;
mod pipe;
mod pivot_root;
mod poll;
mod ppoll;
mod prctl;
mod pread64;
mod preadv;
mod prlimit64;
mod process_madvise;
mod pselect6;
mod ptrace;
mod pwrite64;
mod pwritev;
mod read;
mod readlink;
mod reboot;
mod recvfrom;
mod recvmsg;
mod removexattr;
mod rename;
#[cfg(target_arch = "riscv64")]
mod riscv_flush_icache;
mod riscv_hwprobe;
mod rmdir;
mod rseq;
mod rt_sigaction;
mod rt_sigpending;
mod rt_sigprocmask;
mod rt_sigreturn;
mod rt_sigsuspend;
mod rt_sigtimedwait;
mod sched_affinity;
mod sched_get_priority_max;
mod sched_get_priority_min;
mod sched_getattr;
mod sched_getparam;
mod sched_getscheduler;
mod sched_rr_get_interval;
mod sched_setattr;
mod sched_setparam;
mod sched_setscheduler;
mod sched_yield;
mod seccomp;
pub use seccomp::SockFilter;
mod select;
mod semctl;
mod semget;
mod semop;
mod sendfile;
mod sendmmsg;
mod sendmsg;
mod sendto;
mod set_ioprio;
mod set_priority;
mod set_robust_list;
mod set_tid_address;
mod setdomainname;
mod setfsgid;
mod setfsuid;
mod setgid;
mod setgroups;
mod sethostname;
mod setitimer;
mod setns;
mod setpgid;
mod setregid;
mod setresgid;
mod setresuid;
mod setreuid;
mod setsid;
mod setsockopt;
mod settimeofday;
mod setuid;
mod setxattr;
mod shmat;
mod shmctl;
mod shmdt;
mod shmget;
mod shutdown;
mod sigaltstack;
mod signalfd;
mod socket;
mod socketpair;
mod stat;
mod statfs;
mod statx;
mod symlink;
mod sync;
mod sysinfo;
mod tgkill;
mod time;
mod timer_create;
mod timer_settime;
mod timerfd_create;
mod timerfd_gettime;
mod timerfd_settime;
mod truncate;
mod umask;
mod umount;
mod uname;
mod unlink;
mod unshare;
mod utimens;
mod wait4;
mod waitid;
mod write;

/// This macro is used to define syscall handler.
/// The first param is the number of parameters,
/// The second param is the function name of syscall handler,
/// The third is optional, means the args(if parameter number > 0),
/// The fourth is optional, means if cpu ctx is required.
macro_rules! syscall_handler {
    (0, $fn_name: ident, $args: ident, $ctx: expr) => {
        $fn_name($ctx)
    };
    (0, $fn_name: ident, $args: ident, $ctx: expr, $user_ctx: expr) => {
        $fn_name($ctx, $user_ctx)
    };

    (1, $fn_name: ident, $args: ident, $ctx: expr) => {
        $fn_name($args[0] as _, $ctx)
    };
    (1, $fn_name: ident, $args: ident, $ctx: expr, $user_ctx: expr) => {
        $fn_name($args[0] as _, $ctx, $user_ctx)
    };

    (2, $fn_name: ident, $args: ident, $ctx: expr) => {
        $fn_name($args[0] as _, $args[1] as _, $ctx)
    };
    (2, $fn_name: ident, $args: ident, $ctx: expr, $user_ctx: expr) => {
        $fn_name($args[0] as _, $args[1] as _, $ctx, $user_ctx)
    };

    (3, $fn_name: ident, $args: ident, $ctx: expr) => {
        $fn_name($args[0] as _, $args[1] as _, $args[2] as _, $ctx)
    };
    (3, $fn_name: ident, $args: ident, $ctx: expr, $user_ctx: expr) => {
        $fn_name($args[0] as _, $args[1] as _, $args[2] as _, $ctx, $user_ctx)
    };

    (4, $fn_name: ident, $args: ident, $ctx: expr) => {
        $fn_name(
            $args[0] as _,
            $args[1] as _,
            $args[2] as _,
            $args[3] as _,
            $ctx,
        )
    };
    (4, $fn_name: ident, $args: ident, $ctx: expr, $user_ctx: expr) => {
        $fn_name(
            $args[0] as _,
            $args[1] as _,
            $args[2] as _,
            $args[3] as _,
            $ctx,
            $user_ctx,
        )
    };

    (5, $fn_name: ident, $args: ident, $ctx: expr) => {
        $fn_name(
            $args[0] as _,
            $args[1] as _,
            $args[2] as _,
            $args[3] as _,
            $args[4] as _,
            $ctx,
        )
    };
    (5, $fn_name: ident, $args: ident, $ctx: expr, $user_ctx: expr) => {
        $fn_name(
            $args[0] as _,
            $args[1] as _,
            $args[2] as _,
            $args[3] as _,
            $args[4] as _,
            $ctx,
            $user_ctx,
        )
    };

    (6, $fn_name: ident, $args: ident, $ctx: expr) => {
        $fn_name(
            $args[0] as _,
            $args[1] as _,
            $args[2] as _,
            $args[3] as _,
            $args[4] as _,
            $args[5] as _,
            $ctx,
        )
    };
    (6, $fn_name: ident, $args: ident, $ctx: expr, $user_ctx: expr) => {
        $fn_name(
            $args[0] as _,
            $args[1] as _,
            $args[2] as _,
            $args[3] as _,
            $args[4] as _,
            $args[5] as _,
            $ctx,
            $user_ctx,
        )
    };
}

macro_rules! dispatch_fn_inner {
    ( $args: ident, $ctx: ident, $user_ctx: ident, $handler: ident ( args[ .. $cnt: tt ] ) ) => {
        $crate::syscall::syscall_handler!($cnt, $handler, $args, $ctx)
    };
    ( $args: ident, $ctx: ident, $user_ctx: ident, $handler: ident ( args[ .. $cnt: tt ] , &user_ctx ) ) => {
        $crate::syscall::syscall_handler!($cnt, $handler, $args, $ctx, &$user_ctx)
    };
    ( $args: ident, $ctx: ident, $user_ctx: ident, $handler: ident ( args[ .. $cnt: tt ] , &mut user_ctx ) ) => {
        // `$user_ctx` is already of type `&mut ostd::cpu::UserContext`,
        // so no need to take `&mut` again
        $crate::syscall::syscall_handler!($cnt, $handler, $args, $ctx, $user_ctx)
    };
}

macro_rules! impl_syscall_nums_and_dispatch_fn {
    // $args, $user_ctx, and $dispatcher_name are needed since Rust macro is hygienic
    ( $( $name: ident = $num: literal => $handler: ident $args: tt );* $(;)? ) => {
        // First, define the syscall numbers
        $(
            pub const $name: u64 = $num;
        )*

        // Then, define the dispatcher function
        pub fn syscall_dispatch(
            syscall_number: u64,
            args: [u64; 6],
            ctx: &crate::context::Context,
            user_ctx: &mut ostd::arch::cpu::context::UserContext,
        ) -> $crate::prelude::Result<$crate::syscall::SyscallReturn> {
            match syscall_number {
                $(
                    $num => {
                        $crate::syscall::log_syscall_entry!($name);
                        $crate::syscall::dispatch_fn_inner!(args, ctx, user_ctx, $handler $args)
                    }
                )*
                _ => {
                    ostd::warn!("Unimplemented syscall number: {}", syscall_number);
                    $crate::error::return_errno_with_message!(
                        $crate::error::Errno::ENOSYS,
                        "Syscall was unimplemented"
                    );
                }
            }
        }
    }
}

// Export macros to sub-modules
use dispatch_fn_inner;
use impl_syscall_nums_and_dispatch_fn;
use syscall_handler;

struct SyscallArgument {
    syscall_number: u64,
    args: [u64; 6],
}

/// Syscall return
#[derive(Clone, Copy, Debug)]
enum SyscallReturn {
    /// return isize, this value will be used to set rax
    Return(isize),
    /// does not need to set rax
    NoReturn,
}

impl SyscallArgument {
    fn new_from_context(user_ctx: &UserContext) -> Self {
        let syscall_number = user_ctx.syscall_num() as u64;
        let args = user_ctx.syscall_args().map(|x| x as u64);
        Self {
            syscall_number,
            args,
        }
    }
}

pub fn handle_syscall(ctx: &Context, user_ctx: &mut UserContext) {
    let syscall_frame = SyscallArgument::new_from_context(user_ctx);
    let profile_start = syscall_profile_begin(syscall_frame.syscall_number, ctx);
    syscall_profile_log_process_boundary(syscall_frame.syscall_number, &syscall_frame.args, ctx);

    // seccomp: consult the thread's policy (strict allowlist or BPF filter)
    // before the syscall executes. On a block, deliver SIGSYS and return ENOSYS;
    // an ERRNO action returns the error directly without a signal. If the signal
    // is ignored/blocked, the syscall returns ENOSYS (matching Linux's
    // `secure_computing` behaviour).
    match seccomp::check(
        ctx,
        syscall_frame.syscall_number,
        &syscall_frame.args,
        user_ctx.instruction_pointer(),
    ) {
        seccomp::SeccompDecision::Allow => {}
        seccomp::SeccompDecision::Kill => {
            ctx.posix_thread
                .enqueue_signal(Box::new(seccomp::SigsysSignal::new(
                    syscall_frame.syscall_number as u32,
                )));
            user_ctx.set_syscall_ret(-(Errno::ENOSYS as i32) as usize);
            syscall_profile_end(profile_start);
            return;
        }
        seccomp::SeccompDecision::Errno(errno) => {
            user_ctx.set_syscall_ret((-errno) as usize);
            syscall_profile_end(profile_start);
            return;
        }
    }

    let syscall_return = arch::syscall_dispatch(
        syscall_frame.syscall_number,
        syscall_frame.args,
        ctx,
        user_ctx,
    );

    match syscall_return {
        Ok(return_value) => {
            if let SyscallReturn::Return(return_value) = return_value {
                user_ctx.set_syscall_ret(return_value as usize);
            }
        }
        Err(err) => {
            debug!("syscall return error: {:?}", err);
            let errno = err.error() as i32;
            user_ctx.set_syscall_ret((-errno) as usize)
        }
    }
    syscall_profile_end(profile_start);
}

macro_rules! log_syscall_entry {
    ($syscall_name: tt) => {
        if ostd::log_enabled!(ostd::log::Level::Debug) {
            let syscall_name_str = stringify!($syscall_name);
            let pid = $crate::context::current!().pid();
            let tid = {
                use $crate::process::posix_thread::AsPosixThread;
                $crate::context::current_thread!()
                    .as_posix_thread()
                    .unwrap()
                    .tid()
            };
            ostd::debug!(
                "[pid={}][tid={}][id={}][{}]",
                pid,
                tid,
                $syscall_name,
                syscall_name_str
            );
        }
    };
}

use log_syscall_entry;
