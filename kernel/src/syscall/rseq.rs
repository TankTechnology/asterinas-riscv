// SPDX-License-Identifier: MPL-2.0

use ostd::mm::VmIo;

use super::SyscallReturn;
use crate::{
    prelude::*,
    process::posix_thread::{
        RSEQ_ALIGN, RSEQ_CPU_ID_OFFSET, RSEQ_CPU_ID_UNINITIALIZED, RSEQ_FLAG_UNREGISTER,
        RSEQ_MIN_SIZE, RSEQ_SIG_OFFSET, Rseq,
    },
};

/// `rseq(rseq_ptr, rseq_len, flags, sig)` — register or unregister a
/// restartable-sequence area for the current thread.
///
/// This is a minimal implementation sufficient for glibc (and other libcs) to
/// stop falling back to non-rseq code paths: it validates the area, writes the
/// signature and a stable `cpu_id == 0`, and remembers the area so it can be
/// unregistered on thread exit. Asterinas does not currently migrate a thread
/// across CPUs, so `cpu_id` never needs to change after registration.
pub fn sys_rseq(
    rseq_ptr: Vaddr,
    rseq_len: usize,
    flags: u32,
    sig: u32,
    ctx: &Context,
) -> Result<SyscallReturn> {
    if flags & RSEQ_FLAG_UNREGISTER != 0 {
        // Unregister: mark the previous area (if any) as uninitialized and forget it.
        let mut rseq = ctx.thread_local.rseq().borrow_mut();
        if let Some(rseq) = rseq.take() {
            let _ = ctx
                .user_space()
                .write_val(rseq.ptr + RSEQ_CPU_ID_OFFSET, &RSEQ_CPU_ID_UNINITIALIZED);
        }
        return Ok(SyscallReturn::Return(0));
    }

    if flags != 0 {
        return_errno_with_message!(Errno::EINVAL, "unsupported rseq flags");
    }
    if rseq_ptr == 0 || rseq_len < RSEQ_MIN_SIZE {
        return_errno_with_message!(Errno::EINVAL, "rseq area is null or too small");
    }
    if !rseq_ptr.is_multiple_of(RSEQ_ALIGN) {
        return_errno_with_message!(Errno::EINVAL, "rseq area is not 32-byte aligned");
    }

    // Write the signature and a stable `cpu_id`/`cpu_id_start` of 0 before
    // remembering the area. Errors here are reported to the caller.
    ctx.user_space()
        .write_val(rseq_ptr + RSEQ_SIG_OFFSET, &sig)?;
    ctx.user_space().write_val(rseq_ptr, &0u32)?;
    ctx.user_space()
        .write_val(rseq_ptr + RSEQ_CPU_ID_OFFSET, &0u32)?;

    *ctx.thread_local.rseq().borrow_mut() = Some(Rseq { ptr: rseq_ptr });

    Ok(SyscallReturn::Return(0))
}
