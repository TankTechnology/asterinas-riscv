// SPDX-License-Identifier: MPL-2.0

pub use cgroup_ns::CgroupNamespace;
pub use controller::cpu::{CpuStatKind, charge_cpu_time};
use fs::CgroupFsType;
use inode::CgroupInode;
pub(in crate::fs) use systree_node::CgroupSystem;
pub use systree_node::{CgroupMembership, CgroupNode, CgroupSysNode};

use crate::{
    fs::{
        file::file_table::{RawFileDesc, get_file_fast},
        utils::systree_inode::{SysTreeInodeTy, SysTreeNodeKind},
    },
    prelude::*,
};

/// Resolves a cgroup-v2 directory file descriptor (as passed to `clone3`'s
/// `CLONE_INTO_CGROUP` via its `cgroup` field) to the corresponding
/// [`CgroupNode`].
pub fn cgroup_node_from_fd(fd: u64, ctx: &Context) -> Result<Arc<CgroupNode>> {
    let raw_fd = RawFileDesc::try_from(fd)
        .map_err(|_| Error::with_message(Errno::EBADF, "invalid cgroup file descriptor"))?;

    let mut file_table = ctx.thread_local.borrow_file_table_mut();
    let file = get_file_fast!(&mut file_table, raw_fd.try_into()?);
    let inode_handle = file.as_inode_handle_or_err()?;

    let inode = inode_handle.path().inode();
    let cgroup_inode = inode
        .as_ref()
        .downcast_ref::<CgroupInode>()
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "the fd is not a cgroup directory"))?;

    let SysTreeNodeKind::Branch(branch_node) = cgroup_inode.node_kind() else {
        return_errno_with_message!(Errno::EINVAL, "the cgroup inode is not a directory node");
    };
    let branch_node = branch_node.clone();
    Arc::downcast::<CgroupNode>(branch_node)
        .map_err(|_| Error::with_message(Errno::EINVAL, "cannot resolve the cgroup node"))
}

// Set this module's log prefix for `ostd::log`.
macro_rules! __log_prefix {
    () => {
        "cgroup: "
    };
}

mod cgroup_ns;
mod controller;
mod fs;
mod inode;
mod systree_node;

// This method should be called during kernel file system initialization,
// _after_ `aster_systree::init`.
pub(super) fn init() {
    crate::fs::vfs::registry::register(&CgroupFsType).unwrap();
}
