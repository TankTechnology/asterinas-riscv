// SPDX-License-Identifier: MPL-2.0

//! The `open_tree(2)` mount API used by systemd's namespace setup.

use crate::{
    fs::{
        file::{DetachedMountFile, FileLike, file_table::FdFlags},
        vfs::path::{EmptyPathStr, FsPath},
    },
    prelude::*,
    syscall::SyscallReturn,
};

pub fn sys_open_tree(
    dirfd: i32,
    path_addr: Vaddr,
    flags: u32,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let flags = OpenTreeFlags::try_from(flags)?;
    super::fsopen::check_mount_api_capability(ctx)?;

    let path = ctx
        .user_space()
        .read_cstring(path_addr, super::constants::MAX_FILENAME_LEN)?;
    let path = path.to_string_lossy();
    let fs_path = FsPath::from_fd_at(
        dirfd,
        path.as_ref(),
        EmptyPathStr::AllowIfFlag(flags.bits()),
    )?;
    let source = {
        let fs_ref = ctx.thread_local.borrow_fs();
        let resolver = fs_ref.resolver().read();
        resolver.lookup(&fs_path)?
    };
    let detached_mount =
        source.clone_mount_tree_detached(flags.contains(OpenTreeFlags::AT_RECURSIVE), ctx)?;
    let file = Arc::new(DetachedMountFile::new(detached_mount)) as Arc<dyn FileLike>;
    let fd = ctx
        .thread_local
        .borrow_file_table()
        .unwrap()
        .write()
        .insert(file, FdFlags::from(flags));
    Ok(SyscallReturn::Return(fd.into()))
}

bitflags! {
    struct OpenTreeFlags: u32 {
        const OPEN_TREE_CLONE = 0x0001;
        const AT_EMPTY_PATH = 0x1000;
        const AT_RECURSIVE = 0x8000;
        const OPEN_TREE_CLOEXEC = 0x80000;
    }
}

impl TryFrom<u32> for OpenTreeFlags {
    type Error = Error;

    fn try_from(value: u32) -> Result<Self> {
        let flags = Self::from_bits(value)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown open_tree flags"))?;
        if !flags.contains(Self::OPEN_TREE_CLONE) {
            return_errno_with_message!(
                Errno::EOPNOTSUPP,
                "open_tree without OPEN_TREE_CLONE is not supported"
            );
        }
        Ok(flags)
    }
}

impl From<OpenTreeFlags> for FdFlags {
    fn from(value: OpenTreeFlags) -> Self {
        if value.contains(OpenTreeFlags::OPEN_TREE_CLOEXEC) {
            Self::CLOEXEC
        } else {
            Self::empty()
        }
    }
}
