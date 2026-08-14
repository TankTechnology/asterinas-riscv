// SPDX-License-Identifier: MPL-2.0

use align_ext::AlignExt;

use super::SyscallReturn;
use crate::{prelude::*, vm::vmar::VMAR_CAP_ADDR};

/// Locks a range of pages so they are always resident in memory.
///
/// Asterinas does not implement swapping, so pages are always resident and
/// `mlock` is effectively a no-op after validating the range. The range is
/// still required to be fully mapped, matching Linux's `ENOMEM` on gaps.
pub fn sys_mlock(addr: Vaddr, len: usize, ctx: &Context) -> Result<SyscallReturn> {
    debug!("addr = 0x{:x}, len = 0x{:x}", addr, len);

    if len == 0 {
        return Ok(SyscallReturn::Return(0));
    }

    // Linux rounds the address down and the length up to cover partial pages.
    let aligned_addr = addr.align_down(PAGE_SIZE);
    let offset = addr - aligned_addr;
    let Some(len_with_offset) = len.checked_add(offset) else {
        return_errno_with_message!(Errno::ENOMEM, "the mapping length overflows");
    };
    let aligned_len = len_with_offset.align_up(PAGE_SIZE);
    if aligned_addr
        .checked_add(aligned_len)
        .is_none_or(|end| end > VMAR_CAP_ADDR)
    {
        return_errno_with_message!(Errno::ENOMEM, "the mapping range is not in userspace");
    }
    let addr_range = aligned_addr..aligned_addr + aligned_len;

    let user_space = ctx.user_space();
    let vmar = user_space.vmar();
    if !vmar.query(addr_range).is_fully_mapped() {
        return_errno_with_message!(
            Errno::ENOMEM,
            "the range contains pages that are not mapped"
        );
    }

    Ok(SyscallReturn::Return(0))
}

/// Unlocks a range of pages previously locked with `mlock`.
///
/// As with `mlock`, this is a no-op in a kernel without swap. Unlike `mlock`,
/// `munlock` silently ignores gaps in the range, so only the range bounds are
/// validated.
pub fn sys_munlock(addr: Vaddr, len: usize, _ctx: &Context) -> Result<SyscallReturn> {
    debug!("addr = 0x{:x}, len = 0x{:x}", addr, len);

    if len == 0 {
        return Ok(SyscallReturn::Return(0));
    }

    let aligned_addr = addr.align_down(PAGE_SIZE);
    let offset = addr - aligned_addr;
    let Some(len_with_offset) = len.checked_add(offset) else {
        return_errno_with_message!(Errno::ENOMEM, "the mapping length overflows");
    };
    let aligned_len = len_with_offset.align_up(PAGE_SIZE);
    if aligned_addr
        .checked_add(aligned_len)
        .is_none_or(|end| end > VMAR_CAP_ADDR)
    {
        return_errno_with_message!(Errno::ENOMEM, "the mapping range is not in userspace");
    }

    Ok(SyscallReturn::Return(0))
}
