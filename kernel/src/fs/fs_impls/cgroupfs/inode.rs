// SPDX-License-Identifier: MPL-2.0

use super::fs::CgroupFs;
use crate::{
    fs::{
        cgroupfs::CgroupNode,
        file::InodeMode,
        utils::systree_inode::{SysTreeInodeTy, SysTreeNodeKind},
        vfs::{
            file_system::FileSystem,
            inode::{Extension, Inode, Metadata, RevalidationPolicy},
            inode_ext::InodeExt,
            notify::FsEvents,
            path::{is_dot, is_dotdot},
        },
    },
    prelude::*,
};

/// The canonical events attribute, including its removal state.
///
/// The weak reference avoids a cycle through the inode's owning cgroup node.
/// Its mutex must be released before publishing filesystem events.
pub(super) struct EventsInodeCache(Mutex<EventsInodeState>);

struct EventsInodeState {
    inode: Weak<CgroupInode>,
    removed: bool,
}

impl EventsInodeCache {
    pub(super) fn new() -> Self {
        Self(Mutex::new(EventsInodeState {
            inode: Weak::new(),
            removed: false,
        }))
    }

    fn get_or_init(&self, create: impl FnOnce() -> Arc<CgroupInode>) -> Arc<CgroupInode> {
        let (inode, removed) = {
            let mut state = self.0.lock();
            if let Some(inode) = state.inode.upgrade() {
                // `remove` owns retirement of an existing inode, including
                // when a concurrent lookup observes the removal in progress.
                return inode;
            }
            let inode = create();
            // A deleted node's new inode stays private until retired, so a
            // second lookup cannot subscribe while retirement is pending.
            if !state.removed {
                state.inode = Arc::downgrade(&inode);
            }
            (inode, state.removed)
        };
        if removed {
            Self::retire(&inode);
        }
        inode
    }

    pub(super) fn notify_modified(&self) {
        let inode = self.0.lock().inode.upgrade();
        if let Some(inode) = inode {
            let inode: &dyn Inode = inode.as_ref();
            if let Some(publisher) = inode.fs_event_publisher() {
                publisher.publish_event(FsEvents::MODIFY, None);
            }
        }
    }

    pub(super) fn remove(&self) {
        let inode = {
            let mut state = self.0.lock();
            if state.removed {
                return;
            }
            state.removed = true;
            state.inode.upgrade()
        };
        if let Some(inode) = inode {
            Self::retire(&inode);
        }
    }

    fn retire(inode: &CgroupInode) {
        let inode: &dyn Inode = inode;
        let publisher = inode.fs_event_publisher_or_init();
        publisher.publish_event(FsEvents::DELETE_SELF, None);
        let removed = publisher.disable_new_and_remove_subscribers();
        inode
            .fs()
            .fs_event_subscriber_stats()
            .remove_subscribers(removed);
    }
}

/// An inode abstraction used in the cgroup file system.
pub(super) struct CgroupInode {
    /// The corresponding node in the SysTree.
    node_kind: SysTreeNodeKind,
    /// The metadata of this inode.
    metadata: Metadata,
    /// The extension of this inode.
    extension: Extension,
    /// The file mode (permissions) of this inode, protected by a lock.
    mode: RwLock<InodeMode>,
    /// Weak reference to the parent inode.
    parent: Weak<CgroupInode>,
    /// Weak self-reference for cyclic data structures.
    this: Weak<CgroupInode>,
}

impl SysTreeInodeTy for CgroupInode {
    fn new_arc(
        node_kind: SysTreeNodeKind,
        metadata: Metadata,
        mode: InodeMode,
        parent: Weak<Self>,
    ) -> Arc<Self>
    where
        Self: Sized,
    {
        if let SysTreeNodeKind::Attr(attr, node) = &node_kind
            && attr.name().as_ref() == "cgroup.events"
        {
            let cgroup = Arc::downcast::<CgroupNode>(node.clone()).unwrap();
            return cgroup
                .events_inode
                .get_or_init(|| Self::new_uncached(node_kind, metadata, mode, parent));
        }
        Self::new_uncached(node_kind, metadata, mode, parent)
    }

    fn node_kind(&self) -> &SysTreeNodeKind {
        &self.node_kind
    }

    fn metadata(&self) -> &Metadata {
        &self.metadata
    }

    fn extension(&self) -> &Extension {
        &self.extension
    }

    fn mode(&self) -> Result<InodeMode> {
        Ok(*self.mode.read())
    }

    fn set_mode(&self, mode: InodeMode) -> Result<()> {
        *self.mode.write() = mode;
        Ok(())
    }

    fn parent(&self) -> &Weak<Self> {
        &self.parent
    }

    fn this(&self) -> Arc<Self> {
        self.this
            .upgrade()
            .expect("invalid weak reference to `self`")
    }
}

impl CgroupInode {
    fn new_uncached(
        node_kind: SysTreeNodeKind,
        metadata: Metadata,
        mode: InodeMode,
        parent: Weak<Self>,
    ) -> Arc<Self> {
        Arc::new_cyclic(|this| Self {
            node_kind,
            metadata,
            extension: Extension::new(),
            mode: RwLock::new(mode),
            parent,
            this: this.clone(),
        })
    }
}

impl Inode for CgroupInode {
    fn fs(&self) -> Arc<dyn FileSystem> {
        CgroupFs::singleton().clone()
    }

    fn rmdir(&self, name: &str) -> Result<()> {
        if is_dot(name) {
            return_errno_with_message!(Errno::EINVAL, "rmdir on .");
        }
        if is_dotdot(name) {
            return_errno_with_message!(Errno::ENOTEMPTY, "rmdir on ..");
        }

        let SysTreeNodeKind::Branch(branch_node) = self.node_kind() else {
            return_errno_with_message!(Errno::ENOTDIR, "the current node is not a branch node");
        };

        let Some(child) = branch_node.child(name) else {
            return_errno_with_message!(Errno::ENOENT, "the child node does not exist");
        };

        let target_node = Arc::downcast::<CgroupNode>(child).unwrap();

        // This will succeed only if the child is empty and has not been removed.
        target_node.mark_as_dead()?;

        // This is guaranteed to remove `child` because the dentry lock prevents
        // concurrent modification to the children, and there are no races because
        // `mark_as_dead` can succeed at most once.
        branch_node.remove_child(name).unwrap();

        Ok(())
    }

    fn revalidation_policy(&self) -> RevalidationPolicy {
        match self.node_kind() {
            SysTreeNodeKind::Branch(_) | SysTreeNodeKind::Leaf(_) => {
                RevalidationPolicy::REVALIDATE_EXISTS | RevalidationPolicy::REVALIDATE_ABSENT
            }
            SysTreeNodeKind::Attr(..) | SysTreeNodeKind::Symlink(_) => RevalidationPolicy::empty(),
        }
    }

    fn revalidate_exists(&self, _name: &str, child: &dyn Inode) -> bool {
        // Attribute nodes in the cache should not be trusted because they
        // may be dynamically created or removed based on the state of the
        // cgroup controller.
        child
            .downcast_ref::<Self>()
            .is_some_and(|child| match child.node_kind() {
                // This fixed attribute must retain its inode and inotify watches.
                SysTreeNodeKind::Attr(attr, _) => attr.name().as_ref() == "cgroup.events",
                _ => true,
            })
    }

    fn revalidate_absent(&self, _name: &str) -> bool {
        false
    }
}
