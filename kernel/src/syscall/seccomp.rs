// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    prelude::*,
    process::{
        credentials::capabilities::CapSet,
        posix_thread::{AsPosixThread, SeccompFilter},
        signal::{c_types::siginfo_t, constants::SIGSYS, sig_num::SigNum, signals::Signal},
    },
    security::lsm::hooks as lsm_hooks,
};

/// Seccomp modes (values of the per-thread `seccomp_mode` field).
pub const SECCOMP_MODE_DISABLED: u32 = 0;
pub const SECCOMP_MODE_STRICT: u32 = 1;
pub const SECCOMP_MODE_FILTER: u32 = 2;

// --- `seccomp(2)` operations (`linux/seccomp.h`) ---
const SECCOMP_SET_MODE_STRICT: u32 = 0;
const SECCOMP_SET_MODE_FILTER: u32 = 1;

/// Synchronize a newly installed filter to all threads in the process.
const SECCOMP_FILTER_FLAG_TSYNC: u32 = 1 << 0;

/// `si_code` value for a seccomp-generated `SIGSYS` (`asm-generic/siginfo.h`).
const SYS_SECCOMP: i32 = 1;

/// The syscalls permitted in `SECCOMP_SET_MODE_STRICT`, by riscv64 asm-generic
/// number: `read`, `write`, `exit` and `rt_sigreturn`. `exit_group` is, per the
/// `seccomp(2)` man page, deliberately *not* permitted.
///
/// Reference: <https://man7.org/linux/man-pages/man2/seccomp.2.html>
const STRICT_ALLOWED: &[u64] = &[
    63,  /* read */
    64,  /* write */
    93,  /* exit */
    139, /* rt_sigreturn */
];

// --- seccomp filter return values (`linux/seccomp.h`) ---
const SECCOMP_RET_KILL_THREAD: u32 = 0x0000_0000;
const SECCOMP_RET_KILL_PROCESS: u32 = 0x8000_0000;
const SECCOMP_RET_TRAP: u32 = 0x0003_0000;
const SECCOMP_RET_ERRNO: u32 = 0x0005_0000;
const SECCOMP_RET_LOG: u32 = 0x7ffc_0000;
const SECCOMP_RET_ALLOW: u32 = 0x7fff_0000;
const SECCOMP_RET_ACTION_FULL: u32 = 0xffff_0000;
const SECCOMP_RET_DATA: u32 = 0x0000_ffff;

/// `AUDIT_ARCH_RISCV64` = `EM_RISCV | __AUDIT_ARCH_64BIT | __AUDIT_ARCH_LE`.
const AUDIT_ARCH_RISCV64: u32 = 0xc000_00f3;

/// The outcome of evaluating a thread's seccomp policy for a syscall.
#[derive(Debug, PartialEq)]
pub enum SeccompDecision {
    /// The syscall is allowed to proceed.
    Allow,
    /// The syscall is blocked: deliver `SIGSYS` and return `ENOSYS`.
    Kill,
    /// The syscall is blocked: return the given `-errno` without a signal.
    Errno(i32),
}

/// A classic BPF instruction (`struct sock_filter` in `linux/filter.h`).
pub use crate::util::bpf::SockFilter;

/// Returns whether the calling thread's seccomp policy blocks `syscall_number`.
///
/// Strict mode rejects any syscall outside the strict allowlist. Filter mode
/// evaluates every program in the thread's filter chain against `seccomp_data`
/// and selects the highest-precedence action. A thread with no policy always
/// allows.
pub fn check(
    ctx: &Context,
    syscall_number: u64,
    args: &[u64; 6],
    instruction_pointer: usize,
) -> SeccompDecision {
    match ctx.posix_thread.seccomp_mode() {
        SECCOMP_MODE_STRICT => {
            if STRICT_ALLOWED.contains(&syscall_number) {
                SeccompDecision::Allow
            } else {
                SeccompDecision::Kill
            }
        }
        SECCOMP_MODE_FILTER => {
            let Some(filter) = ctx.posix_thread.seccomp_filter() else {
                return SeccompDecision::Allow;
            };
            let data = build_seccomp_data(syscall_number, args, instruction_pointer);
            seccomp_action_to_decision(run_filter_chain(&filter, &data))
        }
        _ => SeccompDecision::Allow,
    }
}

/// Builds the `seccomp_data` structure (64 bytes on riscv64) that a filter's
/// `BPF_LD | BPF_W | BPF_ABS` instructions read from.
fn build_seccomp_data(
    syscall_number: u64,
    args: &[u64; 6],
    instruction_pointer: usize,
) -> [u8; 64] {
    let mut data = [0u8; 64];
    data[0..4].copy_from_slice(&(syscall_number as i32).to_ne_bytes());
    data[4..8].copy_from_slice(&AUDIT_ARCH_RISCV64.to_ne_bytes());
    data[8..16].copy_from_slice(&(instruction_pointer as u64).to_ne_bytes());
    for (i, arg) in args.iter().enumerate() {
        data[16 + 8 * i..24 + 8 * i].copy_from_slice(&arg.to_ne_bytes());
    }
    data
}

/// Maps a seccomp filter return value to a [`SeccompDecision`].
fn seccomp_action_to_decision(action: u32) -> SeccompDecision {
    match action & SECCOMP_RET_ACTION_FULL {
        SECCOMP_RET_ALLOW | SECCOMP_RET_LOG => SeccompDecision::Allow,
        SECCOMP_RET_ERRNO => SeccompDecision::Errno((action & SECCOMP_RET_DATA) as i32),
        // KILL (thread/process), TRAP, TRACE and USER_NOTIF all block the syscall
        // and deliver `SIGSYS` (there is no ptrace/user-notification support, so
        // the signal path is the closest available behaviour).
        _ => SeccompDecision::Kill,
    }
}

/// Runs a classic-BPF program against `data` and returns the `RET` value.
///
/// The interpreter is shared with socket filters (`crate::util::bpf`);
/// `seccomp_data` is read in native byte order. Any malformed or
/// out-of-bounds instruction yields `SECCOMP_RET_KILL_THREAD` (fail secure).
fn run_filter(prog: &[SockFilter], data: &[u8; 64]) -> u32 {
    crate::util::bpf::run_filter(prog, data, false).unwrap_or(SECCOMP_RET_KILL_THREAD)
}

/// Runs newest-to-oldest and selects the action with the smallest signed
/// action-only value, as Linux's `seccomp_run_filters` does. Equal-precedence
/// actions retain the newest filter's data.
fn run_filter_chain(chain: &SeccompFilter, data: &[u8; 64]) -> u32 {
    let mut selected = SECCOMP_RET_ALLOW;
    let mut current = Some(chain);
    while let Some(filter) = current {
        let action = run_filter(filter.program(), data);
        if ((action & SECCOMP_RET_ACTION_FULL) as i32)
            < ((selected & SECCOMP_RET_ACTION_FULL) as i32)
        {
            selected = action;
        }
        current = filter.parent().map(Arc::as_ref);
    }
    selected
}

/// A `SIGSYS` signal raised by seccomp.
///
/// The `_sigsys` detail fields (`si_call_addr` / `si_syscall` / `si_arch`) are
/// not populated (the `siginfo_t` model does not expose them yet); `si_code` is
/// set to `SYS_SECCOMP` so a proper `SIGSYS` handler can distinguish seccomp
/// kills from other sources.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct SigsysSignal {
    syscall: u32,
}

impl SigsysSignal {
    pub const fn new(syscall: u32) -> Self {
        Self { syscall }
    }
}

impl Signal for SigsysSignal {
    fn num(&self) -> SigNum {
        SIGSYS
    }

    fn to_info(&self) -> siginfo_t {
        let _ = self.syscall;
        siginfo_t::new(SIGSYS, SYS_SECCOMP)
    }
}

pub fn sys_seccomp(
    operation: u32,
    flags: u32,
    args: Vaddr,
    ctx: &Context,
) -> Result<SyscallReturn> {
    debug!(
        "seccomp operation = {}, flags = {:#x}, args = {:#x}",
        operation, flags, args
    );

    match operation {
        SECCOMP_SET_MODE_STRICT => {
            // Strict mode takes no flags and no args.
            if flags != 0 {
                return_errno_with_message!(Errno::EINVAL, "seccomp strict mode takes no flags");
            }
            if args != 0 {
                return_errno_with_message!(Errno::EINVAL, "seccomp strict mode takes no args");
            }
            // Serialize seccomp policy changes with TSYNC's preflight/commit.
            let tasks = ctx.process.tasks().lock();
            if ctx.posix_thread.seccomp_mode() != SECCOMP_MODE_DISABLED {
                return_errno_with_message!(
                    Errno::EINVAL,
                    "cannot replace an existing seccomp policy with strict mode"
                );
            }
            ctx.posix_thread.set_seccomp_mode(SECCOMP_MODE_STRICT);
            drop(tasks);
            Ok(SyscallReturn::Return(0))
        }
        SECCOMP_SET_MODE_FILTER => {
            // `SECCOMP_FILTER_FLAG_NEW_LISTENER` / `SECCOMP_FILTER_FLAG_LOG` /
            // `SECCOMP_FILTER_FLAG_SPEC_ALLOW` / `SECCOMP_FILTER_FLAG_TSYNC_ESRCH` /
            // `SECCOMP_FILTER_FLAG_WAIT_KILLABLE_RECV` are not supported yet.
            if flags & !SECCOMP_FILTER_FLAG_TSYNC != 0 {
                return_errno_with_message!(Errno::EINVAL, "unsupported seccomp filter flags");
            }
            if args == 0 {
                return_errno_with_message!(Errno::EFAULT, "null sock_fprog pointer");
            }

            let filters = crate::util::bpf::read_prog_from_user(args)?;

            // Linux permits installing a filter only after privileges have
            // been made non-increasing, or with CAP_SYS_ADMIN in the caller's
            // current user namespace.  The seccomp ABI reports EACCES rather
            // than the capability hook's usual EPERM on failure.
            if !ctx.posix_thread.credentials().no_new_privs()
                && lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
                    ctx.thread_local.borrow_user_ns().as_ref(),
                    ctx.posix_thread,
                    CapSet::SYS_ADMIN,
                ))
                .is_err()
            {
                return_errno_with_message!(
                    Errno::EACCES,
                    "seccomp filter requires no_new_privs or CAP_SYS_ADMIN"
                );
            }

            let program: Arc<[SockFilter]> = Arc::from(filters.into_boxed_slice());
            let tasks = ctx.process.tasks().lock();
            let caller_no_new_privs = ctx.posix_thread.credentials().no_new_privs();
            let caller_filter = ctx.posix_thread.seccomp_filter();
            if ctx.posix_thread.seccomp_mode() == SECCOMP_MODE_STRICT {
                return_errno_with_message!(
                    Errno::EINVAL,
                    "cannot install a filter after strict seccomp mode"
                );
            }
            let Some(filter) = SeccompFilter::try_new(program, caller_filter.clone()) else {
                return_errno_with_message!(
                    Errno::EINVAL,
                    "seccomp filter chain exceeds MAX_INSNS_PER_PATH"
                );
            };

            if flags & SECCOMP_FILTER_FLAG_TSYNC != 0 {
                // A sibling can catch up if its current chain is an ancestor
                // of the caller's chain. Preflight all siblings before making
                // any changes so a divergent tree produces no partial update.
                if let Some(thread) = tasks
                    .as_slice()
                    .iter()
                    .map(|task| task.as_posix_thread().unwrap())
                    .filter(|thread| thread.tid() != ctx.posix_thread.tid())
                    .find(|thread| match thread.seccomp_mode() {
                        SECCOMP_MODE_DISABLED => false,
                        SECCOMP_MODE_FILTER => !SeccompFilter::is_ancestor(
                            thread.seccomp_filter().as_ref(),
                            caller_filter.as_ref(),
                        ),
                        _ => true,
                    })
                {
                    return Ok(SyscallReturn::Return(thread.tid() as _));
                }

                // Holding the task-set lock prevents concurrent clone/exit
                // from changing the synchronization set between preflight and
                // commit.  Installing a filter cannot fail after this point.
                for thread in tasks
                    .as_slice()
                    .iter()
                    .map(|task| task.as_posix_thread().unwrap())
                {
                    if caller_no_new_privs {
                        thread.set_no_new_privs();
                    }
                    thread.set_seccomp_filter(filter.clone());
                    thread.set_seccomp_mode(SECCOMP_MODE_FILTER);
                }
            } else {
                ctx.posix_thread.set_seccomp_filter(filter);
                ctx.posix_thread.set_seccomp_mode(SECCOMP_MODE_FILTER);
            }
            drop(tasks);
            Ok(SyscallReturn::Return(0))
        }
        _ => return_errno_with_message!(Errno::EINVAL, "unknown seccomp operation"),
    }
}
