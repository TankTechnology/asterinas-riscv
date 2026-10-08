// SPDX-License-Identifier: MPL-2.0

use core::{fmt::Write, sync::atomic::AtomicU64};

use super::TidDirOps;
use crate::{
    events::IoEvents,
    fs::{
        file::{mkmod, AccessMode, PerOpenFileOps, StatusFlags},
        procfs::template::{ProcFile, ProcFileOpsByHandle},
        vfs::{
            file_system::FsFlags,
            inode::{FileOps, Inode},
            path::{Mount, MountNamespace, Path, PathResolver, PerMountFlags},
        },
    },
    prelude::*,
    process::{
        posix_thread::AsPosixThread,
        signal::{PollHandle, Pollable},
    },
    thread::Thread,
};

/// A helper function to create the mount point path for a given mount (used by `mounts`,
/// `mountinfo` and `mountstats`).
pub(super) fn make_mount_point_path(
    is_resolver_root_mount: bool,
    parent: Option<&Arc<Mount>>,
    mount: &Mount,
    path_resolver: &PathResolver,
) -> String {
    if is_resolver_root_mount {
        "/".to_string()
    } else if let Some(parent) = parent {
        if let Some(mount_point_dentry) = mount.mountpoint() {
            path_resolver
                .make_abs_path(&Path::new(parent.clone(), mount_point_dentry))
                .into_string()
        } else {
            "".to_string()
        }
    } else {
        // No parent means it's the root of the namespace.
        "/".to_string()
    }
}

/// A single entry in the mountinfo file.
struct MountInfoEntry<'a> {
    /// The recyclable mount ID (matches [`Mount::id`]); reused after the
    /// mount is dropped.
    mount_id: u32,
    /// The recyclable ID of the parent mount (or self if it has no parent).
    parent_id: u32,
    /// The major device ID of the filesystem.
    major: u32,
    /// The minor device ID of the filesystem.
    minor: u32,
    /// The root of the mount within the filesystem.
    root: &'a str,
    /// The mount point relative to the process's root directory.
    mount_point: &'a str,
    /// Per-mount flags.
    mount_flags: PerMountFlags,
    /// The type of the filesystem in the form "type[.subtype]".
    fs_type: &'a str,
    /// Filesystem-specific information or "none".
    source: &'a str,
    /// Per-filesystem flags.
    fs_flags: FsFlags,
}

impl core::fmt::Display for MountInfoEntry<'_> {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        write!(
            f,
            "{} {} {}:{} {} {} {} - {} {} {}",
            self.mount_id,
            self.parent_id,
            self.major,
            self.minor,
            self.root,
            self.mount_point,
            self.mount_flags,
            self.fs_type,
            self.source,
            self.fs_flags,
        )
    }
}

/// Represents the inode at `/proc/[pid]/task/[tid]/mountinfo` (and also `/proc/[pid]/mountinfo`).
pub struct MountInfoFileOps(TidDirOps);

impl MountInfoFileOps {
    pub fn new_inode(dir: &TidDirOps, parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference: <https://elixir.bootlin.com/linux/v6.16.5/source/fs/proc/base.c#L3352>
        ProcFile::new(Self(dir.clone()), parent, mkmod!(a+r))
    }

    /// Renders mount information for `/proc/[pid]/mountinfo`.
    ///
    /// Provides detailed mount information including mount IDs, parent relationships,
    /// and device numbers.
    fn render_mount_info(path_resolver: &PathResolver) -> Result<String> {
        let mut snapshot = String::new();

        for mount in path_resolver.collect_visible_mounts() {
            let mount_id = mount.id();
            let parent = mount.parent().and_then(|parent| parent.upgrade());
            let parent_id = parent.as_ref().map_or(mount_id, |p| p.id());
            let container_dev_id = mount.fs().sb().container_dev_id;
            let major = container_dev_id.major().get() as u32;
            let minor = container_dev_id.minor().get();
            let is_resolver_root_mount = Arc::ptr_eq(&mount, path_resolver.root().mount_node());
            let root = if is_resolver_root_mount {
                path_resolver.root().dentry().path_name()
            } else {
                mount.root_dentry().path_name()
            };
            let mount_point = make_mount_point_path(
                is_resolver_root_mount,
                parent.as_ref(),
                mount.as_ref(),
                path_resolver,
            );
            let mount_flags = mount.flags();
            let fs_type = mount.fs().name();
            let source = mount.source().unwrap_or("none");
            let fs_flags = mount.fs().flags();

            let entry = MountInfoEntry {
                mount_id,
                parent_id,
                major,
                minor,
                root: &root,
                mount_point: &mount_point,
                mount_flags,
                fs_type,
                source,
                fs_flags,
            };

            writeln!(snapshot, "{}", entry)
                .map_err(|_| Error::with_message(Errno::EIO, "cannot format mountinfo"))?;
        }

        Ok(snapshot)
    }
}

impl ProcFileOpsByHandle for MountInfoFileOps {
    fn owner_thread(&self) -> Option<Arc<Thread>> {
        self.0.thread()
    }

    fn open(
        &self,
        _access_mode: AccessMode,
        _status_flags: StatusFlags,
    ) -> Result<Box<dyn PerOpenFileOps>> {
        let Some(thread) = self.0.thread() else {
            return_errno_with_message!(Errno::ESRCH, "the thread does not exist");
        };
        let posix_thread = thread.as_posix_thread().unwrap();

        let path_resolver = {
            let fs = posix_thread.read_fs();
            fs.resolver().read().clone()
        };
        let mount_namespace = {
            let ns_proxy = posix_thread.ns_proxy().lock();
            let Some(ns_proxy) = ns_proxy.as_ref() else {
                return_errno_with_message!(Errno::ESRCH, "the thread does not exist");
            };
            ns_proxy.mnt_ns().clone()
        };
        let observed_event = AtomicU64::new(mount_namespace.mount_event());

        Ok(Box::new(MountInfoFileHandle {
            mount_namespace,
            observed_event,
            path_resolver,
            snapshot: Mutex::new(None),
        }))
    }
}

/// An open `/proc/*/mountinfo` file pinned to one mount namespace.
struct MountInfoFileHandle {
    mount_namespace: Arc<MountNamespace>,
    observed_event: AtomicU64,
    path_resolver: PathResolver,
    snapshot: Mutex<Option<(u64, String)>>,
}

impl Pollable for MountInfoFileHandle {
    fn poll(&self, mask: IoEvents, poller: Option<&mut PollHandle>) -> IoEvents {
        self.mount_namespace
            .poll_mount_changes(&self.observed_event, mask, poller)
    }
}

impl FileOps for MountInfoFileHandle {
    fn read_at(
        &self,
        offset: usize,
        writer: &mut VmWriter,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        let mount_event = self.mount_namespace.mount_event();
        let mut cached = self.snapshot.lock();
        if !matches!(cached.as_ref(), Some((event, _)) if *event == mount_event) {
            *cached = Some((
                mount_event,
                MountInfoFileOps::render_mount_info(&self.path_resolver)?,
            ));
        }

        let bytes = cached.as_ref().unwrap().1.as_bytes();
        let start = offset.min(bytes.len());
        let end = start + (bytes.len() - start).min(writer.avail());
        let mut reader = VmReader::from(&bytes[start..end]);
        Ok(writer.write_fallible(&mut reader).map_err(|(err, _)| err)?)
    }

    fn write_at(
        &self,
        _offset: usize,
        _reader: &mut VmReader,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EPERM, "`/proc/*/mountinfo` is not writable");
    }
}

impl PerOpenFileOps for MountInfoFileHandle {
    fn check_seekable(&self) -> Result<()> {
        Ok(())
    }

    fn is_offset_aware(&self) -> bool {
        true
    }
}
