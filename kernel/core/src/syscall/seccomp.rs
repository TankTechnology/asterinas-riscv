// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    prelude::*,
    process::signal::{constants::SIGSYS, c_types::siginfo_t, sig_num::SigNum, signals::Signal},
};

/// Seccomp modes (values of the per-thread `seccomp_mode` field).
pub const SECCOMP_MODE_DISABLED: u32 = 0;
pub const SECCOMP_MODE_STRICT: u32 = 1;
pub const SECCOMP_MODE_FILTER: u32 = 2;

// --- `seccomp(2)` operations (`linux/seccomp.h`) ---
const SECCOMP_SET_MODE_STRICT: u32 = 0;
const SECCOMP_SET_MODE_FILTER: u32 = 1;

/// `si_code` value for a seccomp-generated `SIGSYS` (`asm-generic/siginfo.h`).
const SYS_SECCOMP: i32 = 1;

/// The syscalls permitted in `SECCOMP_SET_MODE_STRICT`, by riscv64 asm-generic
/// number: `read`, `write`, `exit` and `rt_sigreturn`. `exit_group` is, per the
/// `seccomp(2)` man page, deliberately *not* permitted.
///
/// Reference: <https://man7.org/linux/man-pages/man2/seccomp.2.html>
const STRICT_ALLOWED: &[u64] = &[63 /* read */, 64 /* write */, 93 /* exit */, 139 /* rt_sigreturn */];

/// Returns whether the calling thread's seccomp policy blocks `syscall_number`.
///
/// Only strict mode is implemented; a thread in filter mode is treated as
/// disabled (filter mode cannot be entered via this kernel).
pub fn should_block(ctx: &Context, syscall_number: u64) -> bool {
    ctx.posix_thread.seccomp_mode() == SECCOMP_MODE_STRICT
        && !STRICT_ALLOWED.contains(&syscall_number)
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

pub fn sys_seccomp(operation: u32, flags: u32, args: Vaddr, ctx: &Context) -> Result<SyscallReturn> {
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
            ctx.posix_thread.set_seccomp_mode(SECCOMP_MODE_STRICT);
            Ok(SyscallReturn::Return(0))
        }
        SECCOMP_SET_MODE_FILTER => {
            return_errno_with_message!(Errno::EINVAL, "seccomp BPF filters are not supported");
        }
        _ => return_errno_with_message!(Errno::EINVAL, "unknown seccomp operation"),
    }
}
