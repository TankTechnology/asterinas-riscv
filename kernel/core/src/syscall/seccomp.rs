// SPDX-License-Identifier: MPL-2.0

//! `seccomp(2)` support.
//!
//! The filter-chain implementation is being re-based onto the current
//! `PosixThread` layout. Until it lands, the syscall reports `ENOSYS`, which
//! user space treats as "seccomp is not available" (the same answer a kernel
//! built without `CONFIG_SECCOMP` gives), and no filter is consulted on the
//! syscall path.

use super::SyscallReturn;
use crate::prelude::*;

/// The decision a seccomp policy makes for one syscall.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(super) enum SeccompDecision {
    Allow,
    Kill,
    Trap,
    Errno(i32),
}

/// Consults the current thread's seccomp policy.
///
/// Without an installed filter every syscall is allowed.
pub(super) fn check(
    _ctx: &Context,
    _syscall_number: u64,
    _args: &[u64; 6],
    _instruction_pointer: usize,
) -> SeccompDecision {
    SeccompDecision::Allow
}

pub(super) fn sys_seccomp(
    _operation: u32,
    _flags: u64,
    _args_ptr: Vaddr,
    _ctx: &Context,
) -> Result<SyscallReturn> {
    return_errno_with_message!(Errno::ENOSYS, "seccomp is not implemented yet");
}

/// The `SIGSYS` signal delivered when a filter rejects a syscall.
#[derive(Clone, Copy, Debug)]
pub(super) struct SigsysSignal(pub(super) u32);

impl SigsysSignal {
    pub(super) const fn new(syscall_number: u32) -> Self {
        Self(syscall_number)
    }
}
