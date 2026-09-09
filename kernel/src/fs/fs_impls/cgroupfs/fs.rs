// SPDX-License-Identifier: MPL-2.0

use alloc::{
    sync::{Arc, Weak},
    vec::Vec,
};

use aster_systree::{EmptyNode, SysBranchNode, SysObj};
use ostd::task::Task;
use spin::Once;

use super::{inode::CgroupInode, systree_node::CgroupSystem};
use crate::{
    error::{Errno, Error},
    fs::{
        Result,
        pseudofs::AnonDeviceId,
        utils::systree_inode::{SysTreeInodeTy, SysTreeNodeKind},
        vfs::{
            file_system::{FileSystem, FsEventSubscriberStats, SuperBlock},
            inode::Inode,
            registry::{FsCreationCtx, FsProperties, FsType},
        },
    },
    process::posix_thread::AsThreadLocal,
};

/// A file system for managing cgroups.
pub(super) struct CgroupFs {
    _anon_device_id: AnonDeviceId,
    sb: SuperBlock,
    fs_event_subscriber_stats: FsEventSubscriberStats,
}

// Magic number for cgroupfs v2 (taken from Linux)
const MAGIC_NUMBER: u64 = 0x63677270;
const BLOCK_SIZE: usize = 4096;
const NAME_MAX: usize = 255;

impl CgroupFs {
    /// Returns the `CgroupFs` singleton.
    pub(super) fn singleton() -> &'static Arc<CgroupFs> {
        static SINGLETON: Once<Arc<CgroupFs>> = Once::new();

        SINGLETON.call_once(Self::new)
    }

    fn new() -> Arc<Self> {
        let anon_device_id =
            AnonDeviceId::acquire().expect("no device ID is available for cgroupfs");
        let sb = SuperBlock::new(MAGIC_NUMBER, BLOCK_SIZE, NAME_MAX, anon_device_id.id());

        Arc::new(Self {
            _anon_device_id: anon_device_id,
            sb,
            fs_event_subscriber_stats: FsEventSubscriberStats::new(),
        })
    }
}

impl FileSystem for CgroupFs {
    fn name(&self) -> &'static str {
        "cgroup2"
    }

    fn sync(&self) -> Result<()> {
        // `CgroupFs` is volatile, sync is a no-op
        Ok(())
    }

    fn root_inode(&self) -> Arc<dyn Inode> {
        let current_task = Task::current().unwrap();
        let thread_local = current_task.as_thread_local().unwrap();
        let ns_proxy = thread_local.borrow_ns_proxy();
        let cgroup_namespace = ns_proxy.unwrap().cgroup_ns();

        CgroupInode::new_root(cgroup_namespace.root_node(), &self.sb)
    }

    fn sb(&self) -> SuperBlock {
        self.sb.clone()
    }

    fn fh_to_inode(&self, fh: &[u8]) -> Result<Arc<dyn Inode>> {
        // The default `encode_file_handle` emits a little-endian `u64` inode
        // number. cgroupfs encodes a branch node's inode number as `node_id << 8`
        // (see `fs::utils::systree_inode::ino`), so recovering the node only
        // requires shifting the ID back out and walking the tree for a match.
        let ino = <[u8; 8]>::try_from(fh)
            .map_err(|_| Error::with_message(Errno::EINVAL, "invalid cgroup file handle"))?;
        let ino = u64::from_le_bytes(ino);
        let node_id = ino >> 8;

        let root: Arc<dyn SysBranchNode> = CgroupSystem::singleton().clone();
        let node = find_branch_node_by_id(root, node_id)
            .ok_or_else(|| Error::with_message(Errno::ESTALE, "stale cgroup file handle"))?;

        let sb = self.sb();
        let inode: Arc<dyn Inode> =
            CgroupInode::new_branch_dir(SysTreeNodeKind::Branch(node), None, Weak::new(), &sb);
        Ok(inode)
    }

    fn fs_event_subscriber_stats(&self) -> &FsEventSubscriberStats {
        &self.fs_event_subscriber_stats
    }
}

/// Searches the cgroup `SysTree` for a branch node with the given `SysNodeId`.
///
/// Node IDs are assigned monotonically and are never reused, so a matching ID
/// is globally unique. cgroup trees are small in practice, making a simple
/// depth-first walk adequate.
fn find_branch_node_by_id(root: Arc<dyn SysBranchNode>, id: u64) -> Option<Arc<dyn SysBranchNode>> {
    if root.id().as_u64() == id {
        return Some(root);
    }

    let mut stack: Vec<Arc<dyn SysObj>> = root.children();
    while let Some(obj) = stack.pop() {
        if obj.id().as_u64() == id {
            return obj.cast_to_branch();
        }
        if let Some(branch) = obj.cast_to_branch() {
            stack.extend(branch.children());
        }
    }
    None
}

pub(super) struct CgroupFsType;

impl FsType for CgroupFsType {
    type Key = ();

    fn name(&self) -> &'static str {
        "cgroup2"
    }

    fn properties(&self) -> FsProperties {
        FsProperties::empty()
    }

    fn create(&self, _fs_creation_ctx: &mut FsCreationCtx) -> Result<Arc<dyn FileSystem>> {
        Ok(CgroupFs::singleton().clone())
    }

    fn sysnode(&self) -> Option<Arc<dyn aster_systree::SysNode>> {
        Some(EmptyNode::new("cgroup".into()))
    }
}
