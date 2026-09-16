// SPDX-License-Identifier: MPL-2.0

use align_ext::AlignExt;

use super::SyscallReturn;
use crate::{
    prelude::*,
    process::{ResourceType, credentials::capabilities::CapSet},
    vm::vmar::VMAR_CAP_ADDR,
};

const MCL_CURRENT: u32 = 1;
const MCL_FUTURE: u32 = 2;
const MCL_ONFAULT: u32 = 4;
const MCL_ALL_FLAGS: u32 = MCL_CURRENT | MCL_FUTURE | MCL_ONFAULT;

/// Locks a range of pages so they are always resident in memory.
///
/// Asterinas does not implement swapping, but it still tracks locked mappings
/// for Linux-compatible accounting and resource-limit enforcement.
pub fn sys_mlock(addr: Vaddr, len: usize, ctx: &Context) -> Result<SyscallReturn> {
    debug!("addr = 0x{:x}, len = 0x{:x}", addr, len);

    let Some(addr_range) = checked_lock_range(addr, len)? else {
        return Ok(SyscallReturn::Return(0));
    };

    let user_space = ctx.user_space();
    let vmar = user_space.vmar();
    vmar.lock_memory(addr_range, locked_memory_limit(ctx)?)?;

    Ok(SyscallReturn::Return(0))
}

/// Unlocks a range of pages previously locked with `mlock`.
///
pub fn sys_munlock(addr: Vaddr, len: usize, ctx: &Context) -> Result<SyscallReturn> {
    debug!("addr = 0x{:x}, len = 0x{:x}", addr, len);

    let Some(addr_range) = checked_lock_range(addr, len)? else {
        return Ok(SyscallReturn::Return(0));
    };

    let user_space = ctx.user_space();
    user_space.vmar().unlock_memory(addr_range)?;

    Ok(SyscallReturn::Return(0))
}

/// Locks current mappings, future mappings, or both.
pub fn sys_mlockall(flags: u32, ctx: &Context) -> Result<SyscallReturn> {
    debug!("flags = 0x{:x}", flags);

    if flags & !MCL_ALL_FLAGS != 0
        || flags & (MCL_CURRENT | MCL_FUTURE) == 0
        || flags == MCL_ONFAULT
    {
        return_errno_with_message!(Errno::EINVAL, "invalid mlockall flags");
    }

    let lock_current = flags & MCL_CURRENT != 0;
    let lock_future = flags & MCL_FUTURE != 0;
    let user_space = ctx.user_space();
    user_space
        .vmar()
        .lock_all_memory(lock_current, lock_future, locked_memory_limit(ctx)?)?;

    Ok(SyscallReturn::Return(0))
}

/// Unlocks every mapping and disables locking for future mappings.
pub fn sys_munlockall(ctx: &Context) -> Result<SyscallReturn> {
    debug!("munlockall");

    let user_space = ctx.user_space();
    user_space.vmar().unlock_all_memory();
    Ok(SyscallReturn::Return(0))
}

fn checked_lock_range(addr: Vaddr, len: usize) -> Result<Option<core::ops::Range<Vaddr>>> {
    if len == 0 {
        return Ok(None);
    }

    // Linux rounds the address down and the length up to cover partial pages.
    let aligned_addr = addr.align_down(PAGE_SIZE);
    let offset = addr - aligned_addr;
    let Some(len_with_offset) = len.checked_add(offset) else {
        return_errno_with_message!(Errno::ENOMEM, "the mapping length overflows");
    };
    let Some(aligned_len) = len_with_offset
        .checked_add(PAGE_SIZE - 1)
        .map(|len| len.align_down(PAGE_SIZE))
    else {
        return_errno_with_message!(Errno::ENOMEM, "the mapping length overflows");
    };
    let Some(end) = aligned_addr.checked_add(aligned_len) else {
        return_errno_with_message!(Errno::ENOMEM, "the mapping range overflows");
    };
    if end > VMAR_CAP_ADDR {
        return_errno_with_message!(Errno::ENOMEM, "the mapping range is not in userspace");
    }

    Ok(Some(aligned_addr..end))
}

pub(super) fn locked_memory_limit(ctx: &Context) -> Result<Option<usize>> {
    if ctx
        .posix_thread
        .credentials()
        .effective_capset()
        .contains(CapSet::IPC_LOCK)
    {
        return Ok(None);
    }

    let limit = ctx
        .process
        .resource_limits()
        .get_rlimit(ResourceType::RLIMIT_MEMLOCK)
        .get_cur();
    if limit == 0 {
        return_errno_with_message!(Errno::EPERM, "locking memory requires CAP_IPC_LOCK");
    }

    Ok(Some(limit.min(usize::MAX as u64) as usize))
}
