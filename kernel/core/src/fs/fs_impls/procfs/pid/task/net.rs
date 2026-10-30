// SPDX-License-Identifier: MPL-2.0

//! Network statistics visible through `/proc/[pid]/net`.

use aster_util::printer::VmPrinter;

use super::TidDirOps;
use crate::{
    fs::{
        file::{InodeType, mkmod},
        procfs::template::{
            ListedEntry, ProcDir, ProcDirOps, ProcFile, ProcFileOps, ReaddirEntry,
            visit_listed_entries,
        },
        vfs::inode::Inode,
    },
    prelude::*,
    thread::Thread,
};

/// The `/proc/[pid]/net` directory, also inherited by `/proc/[pid]/task/[tid]`.
pub(super) struct NetDirOps(TidDirOps);

impl NetDirOps {
    pub(super) fn new_inode(dir: &TidDirOps, parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcDir::new(Self(dir.clone()), parent, mkmod!(a+rx))
    }
}

impl ProcDirOps for NetDirOps {
    fn owner_thread(&self) -> Option<Arc<Thread>> {
        self.0.thread()
    }

    fn lookup_child(&self, this_dir: &ProcDir<Self>, name: &str) -> Result<Arc<dyn Inode>> {
        if name != "dev" {
            return_errno_with_message!(Errno::ENOENT, "the file does not exist");
        }
        Ok(DevFileOps::new_inode(this_dir.this_weak().clone()))
    }

    fn visit_entries_from_offset<'a, F>(&'a self, offset: usize, visit_fn: F) -> Result<()>
    where
        F: FnMut(ReaddirEntry<'a>) -> Result<()>,
    {
        visit_listed_entries(offset, [ListedEntry::new("dev", InodeType::File)], visit_fn)
    }
}

struct DevFileOps;

impl DevFileOps {
    fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for DevFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(
            printer,
            "Inter-|   Receive                                                |  Transmit"
        )?;
        writeln!(
            printer,
            " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed"
        )?;

        for (name, stats) in crate::net::iface_stats() {
            // Only packet and byte counters are tracked today. The other Linux
            // columns remain zero until their corresponding events are tracked.
            let fields = [
                stats.rx_bytes,
                stats.rx_packets,
                0,
                0,
                0,
                0,
                0,
                0,
                stats.tx_bytes,
                stats.tx_packets,
                0,
                0,
                0,
                0,
                0,
                0,
            ];
            write!(printer, "{:>6}:", name)?;
            for value in fields {
                write!(printer, " {:>8}", value)?;
            }
            writeln!(printer)?;
        }

        Ok(printer.bytes_written())
    }
}
