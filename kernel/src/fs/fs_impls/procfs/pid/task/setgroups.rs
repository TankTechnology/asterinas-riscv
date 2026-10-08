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
    process::{credentials::capabilities::CapSet, posix_thread::AsPosixThread},
    security::lsm::hooks as lsm_hooks,
    thread::Thread,
};

/// Represents the inode at `/proc/[pid]/task/[tid]/setgroups` (and also
/// `/proc/[pid]/setgroups`).
///
/// Writing "deny" permanently disables `setgroups(2)` for processes in the
/// target user namespace and unlocks writing an unprivileged `gid_map`.
/// It can only be written before the GID map is set.
pub struct SetgroupsFileOps(TidDirOps);

impl SetgroupsFileOps {
    pub fn new_inode(dir: &TidDirOps, parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference: <https://elixir.bootlin.com/linux/v6.16.5/source/fs/proc/base.c#L3401>
        ProcFile::new(Self(dir.clone()), parent, mkmod!(a+r, u+w))
    }
}

impl ProcFileOps for SetgroupsFileOps {
    fn owner_thread(&self) -> Option<Arc<Thread>> {
        self.0.thread()
    }

    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let Some(process) = self.0.process() else {
            return_errno_with_message!(Errno::ESRCH, "the process does not exist");
        };

        let mut printer = VmPrinter::new_skip(writer, offset);

        let user_ns = process.user_ns().lock();
        if user_ns.is_setgroups_denied() {
            writeln!(printer, "deny")?;
        } else {
            writeln!(printer, "allow")?;
        }

        Ok(printer.bytes_written())
    }

    fn write_at(&self, _offset: usize, reader: &mut VmReader) -> Result<usize> {
        let Some(process) = self.0.process() else {
            return_errno_with_message!(Errno::ESRCH, "the process does not exist");
        };

        let (content, len) = reader
            .read_cstring_until_end(PAGE_SIZE)
            .map_err(|_| Error::with_message(Errno::EFAULT, "failed to read the value"))?;
        let value = content
            .to_str()
            .map_err(|_| Error::with_message(Errno::EINVAL, "the value is not valid UTF-8"))?
            .trim();

        let user_ns = process.user_ns().lock().clone();
        let Some(parent_ns) = user_ns.parent_ns().cloned() else {
            return_errno_with_message!(
                Errno::EPERM,
                "setgroups cannot be modified in the initial user namespace"
            );
        };
        match value {
            "deny" => user_ns.deny_setgroups()?,
            "allow" => {
                // Re-enabling setgroups requires CAP_SETGID in the parent
                // user namespace, matching Linux.
                let current_thread = current_thread!();
                lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
                    parent_ns.as_ref(),
                    current_thread.as_posix_thread().unwrap(),
                    CapSet::SETGID,
                ))?;
            }
            _ => return_errno_with_message!(Errno::EINVAL, "unsupported setgroups value"),
        }

        Ok(len)
    }
}
