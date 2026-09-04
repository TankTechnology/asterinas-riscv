// SPDX-License-Identifier: MPL-2.0

use core::cmp::min;

use super::SyscallReturn;
use crate::{prelude::*, vm::vmar::VMAR_CAP_ADDR};

const MAX_RESIDENCY_BYTES_PER_CHUNK: usize = PAGE_SIZE;

/// Reports whether pages in a mapping are resident in memory.
///
/// Reference: <https://man7.org/linux/man-pages/man2/mincore.2.html>.
pub fn sys_mincore(
    addr: Vaddr,
    len: usize,
    vec_addr: Vaddr,
    ctx: &Context,
) -> Result<SyscallReturn> {
    debug!(
        "addr = 0x{:x}, len = 0x{:x}, vec_addr = 0x{:x}",
        addr, len, vec_addr
    );

    if !addr.is_multiple_of(PAGE_SIZE) {
        return_errno_with_message!(Errno::EINVAL, "the mapping address is not aligned");
    }
    if VMAR_CAP_ADDR.checked_sub(addr).is_none_or(|gap| gap < len) {
        return_errno_with_message!(Errno::ENOMEM, "the mapping range is not in userspace");
    }

    let page_count = len / PAGE_SIZE + usize::from(!len.is_multiple_of(PAGE_SIZE));
    let user_space = ctx.user_space();
    let mut vec_writer = user_space.writer(vec_addr, page_count)?;
    if page_count == 0 {
        return Ok(SyscallReturn::Return(0));
    }

    let fsuid = ctx.posix_thread.credentials().fsuid();
    let vmar = user_space.vmar();
    let mut residency = vec![0_u8; min(page_count, MAX_RESIDENCY_BYTES_PER_CHUNK)];
    let mut processed_pages = 0;

    while processed_pages < page_count {
        let chunk_pages = min(residency.len(), page_count - processed_pages);
        let chunk_start = addr + processed_pages * PAGE_SIZE;
        let chunk_end = chunk_start + chunk_pages * PAGE_SIZE;
        {
            let query_guard = vmar.query(chunk_start..chunk_end);
            query_guard.fill_page_residency(&mut residency[..chunk_pages], fsuid)?;
        }

        let mut reader = VmReader::from(&residency[..chunk_pages]);
        vec_writer.write_fallible(&mut reader)?;
        processed_pages += chunk_pages;
    }

    Ok(SyscallReturn::Return(0))
}
