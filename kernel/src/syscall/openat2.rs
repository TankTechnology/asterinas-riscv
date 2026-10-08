// SPDX-License-Identifier: MPL-2.0

use ostd::mm::VmIo;

use super::{SyscallReturn, open::sys_openat};
use crate::{
    fs::file::{CreationFlags, file_table::RawFileDesc},
    prelude::*,
    syscall::constants::MAX_FILENAME_LEN,
};

/// Linux's `struct open_how`.
///
/// Reference: <https://man7.org/linux/man-pages/man2/openat2.2.html>
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct OpenHow {
    flags: u64,
    mode: u64,
    resolve: u64,
}

const _: () = assert!(size_of::<OpenHow>() == 24);

/// `RESOLVE_*` bits from `linux/openat2.h`.
const RESOLVE_NO_XDEV: u64 = 0x01;
const RESOLVE_NO_MAGICLINKS: u64 = 0x02;
const RESOLVE_NO_SYMLINKS: u64 = 0x04;
const RESOLVE_BENEATH: u64 = 0x08;
const RESOLVE_IN_ROOT: u64 = 0x10;
const RESOLVE_CACHED: u64 = 0x20;

/// All `RESOLVE_*` bits defined by Linux.
const RESOLVE_KNOWN: u64 = RESOLVE_NO_XDEV
    | RESOLVE_NO_MAGICLINKS
    | RESOLVE_NO_SYMLINKS
    | RESOLVE_BENEATH
    | RESOLVE_IN_ROOT
    | RESOLVE_CACHED;

/// The subset of `RESOLVE_*` bits this implementation actually honours.
///
/// `RESOLVE_NO_SYMLINKS` is approximated by `O_NOFOLLOW`, which forbids
/// following the *final* component of the path if it is a symlink; the
/// stronger all-components semantics is left as a follow-up. The remaining
/// bits (`NO_XDEV`, `NO_MAGICLINKS`, `BENEATH`, `IN_ROOT`, `CACHED`) require
/// resolution-context tracking that Asterinas does not yet implement, so they
/// are rejected with `EINVAL` (programs then fall back to `openat`).
const RESOLVE_SUPPORTED: u64 = RESOLVE_NO_SYMLINKS;

pub fn sys_openat2(
    dirfd: RawFileDesc,
    path_addr: Vaddr,
    how_addr: Vaddr,
    size: usize,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let path = ctx.user_space().read_cstring(path_addr, MAX_FILENAME_LEN)?;
    debug!(
        "openat2: dirfd = {}, path = {:?}, how_addr = 0x{:x}, size = {}",
        dirfd, path, how_addr, size
    );

    if size < size_of::<OpenHow>() {
        return_errno_with_message!(Errno::EINVAL, "open_how size is too small");
    }

    let how: OpenHow = ctx.user_space().read_val(how_addr)?;
    if how.resolve & !RESOLVE_KNOWN != 0 {
        return_errno_with_message!(Errno::EINVAL, "unknown RESOLVE flag in open_how");
    }
    if how.resolve & !RESOLVE_SUPPORTED != 0 {
        return_errno_with_message!(Errno::EINVAL, "RESOLVE flag is not yet supported");
    }

    let mut flags = how.flags as u32;
    if how.resolve & RESOLVE_NO_SYMLINKS != 0 {
        flags |= CreationFlags::O_NOFOLLOW.bits();
    }

    sys_openat(dirfd, path_addr, flags, how.mode as u16, ctx)
}
