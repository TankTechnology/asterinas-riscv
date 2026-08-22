// SPDX-License-Identifier: MPL-2.0

use aster_util::printer::VmPrinter;

use super::{
    TidDirOps,
    uid_map::{IdMapKind, write_id_map},
};
use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
    thread::Thread,
};

/// Represents the inode at `/proc/[pid]/task/[tid]/gid_map` (and also `/proc/[pid]/gid_map`).
pub struct GidMapFileOps(TidDirOps);

impl GidMapFileOps {
    pub fn new_inode(dir: &TidDirOps, parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference: <https://elixir.bootlin.com/linux/v6.16.5/source/fs/proc/base.c#L3403>
        ProcFile::new(Self(dir.clone()), parent, mkmod!(a+r, u+w))
    }
}

impl ProcFileOps for GidMapFileOps {
    fn owner_thread(&self) -> Option<Arc<Thread>> {
        self.0.thread()
    }

    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let Some(process) = self.0.process() else {
            return_errno_with_message!(Errno::ESRCH, "the process does not exist");
        };

        let mut printer = VmPrinter::new_skip(writer, offset);

        let user_ns = process.user_ns().lock();
        for extent in user_ns.lock_gid_map().extents() {
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

        write_id_map(&process, reader, IdMapKind::Gid)
    }
}
