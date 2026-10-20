// SPDX-License-Identifier: MPL-2.0

use align_ext::AlignExt;

use super::SyscallReturn;
use crate::{
    fs::{file::SyncMode, vfs::inode::Inode},
    prelude::*,
    thread::kernel_thread::ThreadOptions,
    vm::vmar::VMAR_CAP_ADDR,
};

pub(super) fn sys_msync(
    addr: Vaddr,
    len: usize,
    flag: i32,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let flags = MsyncFlags::from_bits(flag)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "invalid flags"))?;
    debug!("addr = 0x{:x}, len = {}, flags = {:?}", addr, len, flags);

    if flags.contains(MsyncFlags::MS_ASYNC | MsyncFlags::MS_SYNC) {
        return_errno_with_message!(
            Errno::EINVAL,
            "MS_ASYNC and MS_SYNC cannot be specified together"
        );
    }

    if !addr.is_multiple_of(PAGE_SIZE) {
        return_errno_with_message!(Errno::EINVAL, "the mapping address is not aligned");
    }
    if len == 0 {
        return Ok(SyscallReturn::Return(0));
    }
    if VMAR_CAP_ADDR.checked_sub(addr).is_none_or(|gap| gap < len) {
        // FIXME: Linux returns `ENOMEM` if `(addr + len).align_up(PAGE_SIZE)` overflows. Here, we
        // perform a stricter validation.
        return_errno_with_message!(Errno::ENOMEM, "the mapping range is not in userspace");
    }
    let addr_range = addr..(addr + len).align_up(PAGE_SIZE);

    let user_space = ctx.user_space();
    let vmar = user_space.vmar();
    let query_guard = vmar.query(addr_range.clone());

    // Check if the range is fully mapped.
    if !query_guard.is_fully_mapped() {
        return_errno_with_message!(
            Errno::ENOMEM,
            "the range contains pages that are not mapped"
        );
    }

    // Do nothing if not file-backed, as <https://pubs.opengroup.org/onlinepubs/9699919799/> says.
    let inodes = query_guard
        .iter()
        .filter_map(|m| m.inode().cloned())
        .collect::<Vec<_>>();

    // If neither MS_SYNC nor MS_ASYNC is specified, Linux defaults to MS_ASYNC behavior.
    if flags.contains(MsyncFlags::MS_SYNC) {
        sync_inodes(&inodes)?;
    } else {
        ThreadOptions::new(move || {
            // An asynchronous request cannot report a failure to its caller.
            let _ = sync_inodes(&inodes);
        })
        .spawn();
    }

    Ok(SyscallReturn::Return(0))
}

/// Syncs every file-backed mapping in the range, reporting the first failure.
fn sync_inodes(inodes: &[Arc<dyn Inode>]) -> Result<()> {
    let mut first_error = None;
    for inode in inodes {
        // TODO: Sync a necessary range instead of the whole inode.
        if let Err(err) = inode.sync(SyncMode::Full)
            && first_error.is_none()
        {
            first_error = Some(err);
        }
    }
    match first_error {
        Some(err) => Err(err),
        None => Ok(()),
    }
}

bitflags! {
    /// Flags for `msync`.
    ///
    /// Reference: <https://elixir.bootlin.com/linux/v6.15.1/source/include/uapi/asm-generic/mman-common.h#L42>.
    struct MsyncFlags: i32 {
        /// Performs `msync` asynchronously.
        const MS_ASYNC      = 0x01;
        /// Invalidates cache so that other processes mapping the same file
        /// will immediately see the changes after this `msync` call.
        ///
        /// Should be a no-op since we use the same page cache for all processes.
        const MS_INVALIDATE = 0x02;
        /// Performs `msync` synchronously.
        const MS_SYNC       = 0x04;
    }
}
