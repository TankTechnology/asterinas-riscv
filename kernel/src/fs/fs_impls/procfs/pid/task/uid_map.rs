// SPDX-License-Identifier: MPL-2.0

use aster_util::printer::VmPrinter;

use super::TidDirOps;
use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
    process::{
        IdMapExtent, Process, credentials::capabilities::CapSet, posix_thread::AsPosixThread,
    },
    security::lsm::hooks as lsm_hooks,
    thread::Thread,
};

/// Represents the inode at `/proc/[pid]/task/[tid]/uid_map` (and also `/proc/[pid]/uid_map`).
pub struct UidMapFileOps(TidDirOps);

impl UidMapFileOps {
    pub fn new_inode(dir: &TidDirOps, parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference: <https://elixir.bootlin.com/linux/v6.16.5/source/fs/proc/base.c#L3402>
        ProcFile::new(Self(dir.clone()), parent, mkmod!(a+r, u+w))
    }
}

impl ProcFileOps for UidMapFileOps {
    fn owner_thread(&self) -> Option<Arc<Thread>> {
        self.0.thread()
    }

    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let Some(process) = self.0.process() else {
            return_errno_with_message!(Errno::ESRCH, "the process does not exist");
        };

        let mut printer = VmPrinter::new_skip(writer, offset);

        let user_ns = process.user_ns().lock();
        for extent in user_ns.lock_uid_map().extents() {
            // Note: `lower_first` is stored as a global kernel ID. This is
            // exactly the parent-namespace view for first-level namespaces;
            // for nested namespaces the parent map should be applied, which
            // is not implemented yet.
            writeln!(
                printer,
                "{:>10} {:>10} {:>10}",
                extent.first, extent.lower_first, extent.count
            )?;
        }

        Ok(printer.bytes_written())
    }

    fn write_at(&self, _offset: usize, reader: &mut VmReader) -> Result<usize> {
        let Some(process) = self.0.process() else {
            return_errno_with_message!(Errno::ESRCH, "the process does not exist");
        };

        write_id_map(&process, reader, IdMapKind::Uid)
    }
}

/// Whether a `/proc/[pid]/*id_map` file maps user or group IDs.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum IdMapKind {
    Uid,
    Gid,
}

/// Parses the ID map from the reader, returning the extents as
/// `(first, lower_first, count)` triples and the number of bytes read.
///
/// `lower_first` values are expressed in the *parent* user namespace.
fn parse_id_map(reader: &mut VmReader) -> Result<(Vec<(u32, u32, u32)>, usize)> {
    let (content, len) = reader
        .read_cstring_until_end(PAGE_SIZE)
        .map_err(|_| Error::with_message(Errno::EFAULT, "failed to read the ID map"))?;
    let content = content
        .to_str()
        .map_err(|_| Error::with_message(Errno::EINVAL, "the ID map is not valid UTF-8"))?;

    let mut extents = Vec::new();
    for line in content.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }

        let mut parts = line.split_whitespace();
        let (Some(first), Some(lower_first), Some(count), None) =
            (parts.next(), parts.next(), parts.next(), parts.next())
        else {
            return_errno_with_message!(Errno::EINVAL, "malformed ID map line");
        };
        let (Ok(first), Ok(lower_first), Ok(count)) = (
            first.parse::<u32>(),
            lower_first.parse::<u32>(),
            count.parse::<u32>(),
        ) else {
            return_errno_with_message!(Errno::EINVAL, "malformed ID map line");
        };
        if count == 0
            || first.checked_add(count).is_none()
            || lower_first.checked_add(count).is_none()
        {
            return_errno_with_message!(Errno::EINVAL, "ID map extent out of range");
        }

        extents.push((first, lower_first, count));
    }

    if extents.is_empty() {
        return_errno_with_message!(Errno::EINVAL, "the ID map is empty");
    }

    Ok((extents, len))
}

/// Writes the UID or GID map of `target_process`'s user namespace.
///
/// Permission model (a simplification of Linux's `new_idmap`):
/// - a writer with `CAP_SETUID`/`CAP_SETGID` in the parent user namespace
///   may write arbitrary extents;
/// - an unprivileged process may write *its own* map once, with a single
///   extent that maps its own effective ID;
/// - GID maps additionally require `setgroups` to be denied first.
pub(super) fn write_id_map(
    target_process: &Arc<Process>,
    reader: &mut VmReader,
    kind: IdMapKind,
) -> Result<usize> {
    let (raw_extents, len) = parse_id_map(reader)?;

    let target_ns = target_process.user_ns().lock().clone();
    let Some(parent_ns) = target_ns.parent_ns().cloned() else {
        return_errno_with_message!(
            Errno::EPERM,
            "cannot write the ID map of the initial user namespace"
        );
    };

    let current_thread = current_thread!();
    let current_posix = current_thread.as_posix_thread().unwrap();
    let credentials = current_posix.credentials();

    let required_cap = match kind {
        IdMapKind::Uid => CapSet::SETUID,
        IdMapKind::Gid => CapSet::SETGID,
    };
    let has_cap = lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
        parent_ns.as_ref(),
        current_posix,
        required_cap,
    ))
    .is_ok();

    let is_self = Arc::ptr_eq(&current_posix.process(), target_process);

    if !has_cap {
        if !is_self || raw_extents.len() != 1 {
            return_errno_with_message!(
                Errno::EPERM,
                "writing the ID map requires the corresponding capability in the parent user namespace"
            );
        }

        // The extent must map the writer's own effective ID.
        let writer_id = match kind {
            IdMapKind::Uid => u32::from(credentials.euid()),
            IdMapKind::Gid => u32::from(credentials.egid()),
        };
        let parent_map = match kind {
            IdMapKind::Uid => parent_ns.lock_uid_map(),
            IdMapKind::Gid => parent_ns.lock_gid_map(),
        };
        if parent_map.map_up(raw_extents[0].1) != Some(writer_id) {
            return_errno_with_message!(
                Errno::EPERM,
                "an unprivileged process may only map its own effective ID"
            );
        }
    }

    if kind == IdMapKind::Gid && !has_cap && !target_ns.is_setgroups_denied() {
        return_errno_with_message!(
            Errno::EACCES,
            "writing gid_map requires denying setgroups first"
        );
    }

    // Translate `lower_first` from the parent-namespace view to global
    // kernel IDs.
    let parent_map = match kind {
        IdMapKind::Uid => parent_ns.lock_uid_map(),
        IdMapKind::Gid => parent_ns.lock_gid_map(),
    };
    let mut extents = Vec::with_capacity(raw_extents.len());
    for (first, lower_first, count) in raw_extents {
        let Some(lower_kid) = parent_map.map_up(lower_first) else {
            return_errno_with_message!(
                Errno::EINVAL,
                "ID map extent is not mapped in the parent user namespace"
            );
        };
        if lower_kid.checked_add(count).is_none() {
            return_errno_with_message!(Errno::EINVAL, "ID map extent out of range");
        }
        extents.push(IdMapExtent {
            first,
            lower_first: lower_kid,
            count,
        });
    }
    drop(parent_map);

    match kind {
        IdMapKind::Uid => target_ns.lock_uid_map().write(extents)?,
        IdMapKind::Gid => target_ns.lock_gid_map().write(extents)?,
    }

    Ok(len)
}
