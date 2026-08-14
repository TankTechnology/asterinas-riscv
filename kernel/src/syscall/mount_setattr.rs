// SPDX-License-Identifier: MPL-2.0

use ostd::mm::VmIo;

use super::SyscallReturn;
use crate::{
    fs::{
        file::file_table::RawFileDesc,
        vfs::path::{EmptyPathStr, FsPath, MountPropType, PerMountFlags},
    },
    prelude::*,
    syscall::constants::MAX_FILENAME_LEN,
};

/// `flags` bit: apply the change to the whole mount subtree.
const AT_RECURSIVE: u32 = 0x8000;

/// `mount_attr::attr_set` / `attr_clr` bits (`linux/mount.h`).
const MOUNT_ATTR_RDONLY: u64 = 0x0000_0001;
const MOUNT_ATTR_NOSUID: u64 = 0x0000_0002;
const MOUNT_ATTR_NODEV: u64 = 0x0000_0004;
const MOUNT_ATTR_NOEXEC: u64 = 0x0000_0008;
/// Mask of the atime-mode bits (`NOATIME | STRICTATIME` plus a reserved bit).
const MOUNT_ATTR__ATIME: u64 = 0x0000_0070;
const MOUNT_ATTR_NOATIME: u64 = 0x0000_0010;
const MOUNT_ATTR_STRICTATIME: u64 = 0x0000_0020;
const MOUNT_ATTR_NODIRATIME: u64 = 0x0000_0080;
const MOUNT_ATTR_IDMAP: u64 = 0x0010_0000;
const MOUNT_ATTR_NOSYMFOLLOW: u64 = 0x0020_0000;

const MOUNT_ATTR_KNOWN: u64 = MOUNT_ATTR_RDONLY
    | MOUNT_ATTR_NOSUID
    | MOUNT_ATTR_NODEV
    | MOUNT_ATTR_NOEXEC
    | MOUNT_ATTR__ATIME
    | MOUNT_ATTR_NODIRATIME
    | MOUNT_ATTR_IDMAP
    | MOUNT_ATTR_NOSYMFOLLOW;

/// `mount_attr::propagation` bits (`MS_*` from `sys/mount.h`).
const MS_UNBINDABLE: u64 = 1 << 17;
const MS_PRIVATE: u64 = 1 << 18;
const MS_SLAVE: u64 = 1 << 19;
const MS_SHARED: u64 = 1 << 20;
const MS_PROPAGATION: u64 = MS_UNBINDABLE | MS_PRIVATE | MS_SLAVE | MS_SHARED;

/// Linux's `struct mount_attr`.
///
/// Reference: <https://man7.org/linux/man-pages/man2/mount_setattr.2.html>
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct MountAttr {
    attr_set: u64,
    attr_clr: u64,
    propagation: u64,
    userns_fd: u64,
}

const _: () = assert!(size_of::<MountAttr>() == 32);

pub fn sys_mount_setattr(
    dfd: RawFileDesc,
    path_addr: Vaddr,
    flags: u32,
    attr_addr: Vaddr,
    size: usize,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let user_space = ctx.user_space();
    let path = user_space.read_cstring(path_addr, MAX_FILENAME_LEN)?;
    debug!(
        "mount_setattr: dfd = {}, path = {:?}, flags = 0x{:x}, attr_addr = 0x{:x}, size = {}",
        dfd, path, flags, attr_addr, size,
    );

    if flags & !AT_RECURSIVE != 0 {
        return_errno_with_message!(Errno::EINVAL, "invalid mount_setattr flags");
    }
    let recursive = flags & AT_RECURSIVE != 0;

    if size < size_of::<MountAttr>() {
        return_errno_with_message!(Errno::EINVAL, "mount_attr size is too small");
    }
    let attr: MountAttr = user_space.read_val(attr_addr)?;

    if attr.attr_set & !MOUNT_ATTR_KNOWN != 0 || attr.attr_clr & !MOUNT_ATTR_KNOWN != 0 {
        return_errno_with_message!(Errno::EINVAL, "unknown mount_attr attribute");
    }
    if attr.attr_set & attr.attr_clr != 0 {
        return_errno_with_message!(Errno::EINVAL, "attr_set and attr_clr overlap");
    }
    if attr.attr_set & MOUNT_ATTR_IDMAP != 0
        || attr.attr_clr & MOUNT_ATTR_IDMAP != 0
        || attr.userns_fd != 0
    {
        return_errno_with_message!(Errno::EOPNOTSUPP, "id-mapped mounts are not supported");
    }
    // Only one atime mode may be requested at a time.
    if (attr.attr_set & MOUNT_ATTR__ATIME).count_ones() > 1
        || (attr.attr_clr & MOUNT_ATTR__ATIME).count_ones() > 1
    {
        return_errno_with_message!(Errno::EINVAL, "conflicting atime flags");
    }

    let target_path = {
        let path = path.to_string_lossy();
        let fs_path = FsPath::from_fd_at(dfd, &path, EmptyPathStr::Reject)?;
        ctx.thread_local
            .borrow_fs()
            .resolver()
            .read()
            .lookup(&fs_path)?
            .get_top_path()
    };

    if attr.propagation != 0 {
        if attr.propagation & !MS_PROPAGATION != 0 || attr.propagation.count_ones() != 1 {
            return_errno_with_message!(Errno::EINVAL, "invalid mount propagation value");
        }
        let prop = match attr.propagation {
            MS_PRIVATE => MountPropType::Private,
            MS_SHARED => MountPropType::Shared,
            MS_SLAVE => MountPropType::Slave,
            MS_UNBINDABLE => MountPropType::Unbindable,
            _ => unreachable!(),
        };
        target_path.set_mount_propagation(prop, recursive, ctx)?;
    }

    if attr.attr_set != 0 || attr.attr_clr != 0 {
        let current = target_path.mount_node().flags();
        let set = mount_attr_to_per_mount_flags(attr.attr_set);
        let clr = mount_attr_to_per_mount_flags(attr.attr_clr);
        let new_flags = (current | set) & !clr;
        target_path.remount(new_flags, None, None, ctx)?;
    }

    Ok(SyscallReturn::Return(0))
}

/// Maps the `MOUNT_ATTR_*` bit values to their `PerMountFlags` (`MS_*`) bit
/// values, which differ for the atime and symlink-follow bits.
fn mount_attr_to_per_mount_flags(attrs: u64) -> PerMountFlags {
    let mut flags = PerMountFlags::empty();
    if attrs & MOUNT_ATTR_RDONLY != 0 {
        flags |= PerMountFlags::RDONLY;
    }
    if attrs & MOUNT_ATTR_NOSUID != 0 {
        flags |= PerMountFlags::NOSUID;
    }
    if attrs & MOUNT_ATTR_NODEV != 0 {
        flags |= PerMountFlags::NODEV;
    }
    if attrs & MOUNT_ATTR_NOEXEC != 0 {
        flags |= PerMountFlags::NOEXEC;
    }
    if attrs & MOUNT_ATTR_NOATIME != 0 {
        flags |= PerMountFlags::NOATIME;
    }
    if attrs & MOUNT_ATTR_STRICTATIME != 0 {
        flags |= PerMountFlags::STRICTATIME;
    }
    if attrs & MOUNT_ATTR_NODIRATIME != 0 {
        flags |= PerMountFlags::NODIRATIME;
    }
    if attrs & MOUNT_ATTR_NOSYMFOLLOW != 0 {
        flags |= PerMountFlags::NOSYMFOLLOW;
    }
    flags
}
