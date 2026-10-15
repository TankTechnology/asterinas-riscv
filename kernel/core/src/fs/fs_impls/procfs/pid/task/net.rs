// SPDX-License-Identifier: MPL-2.0

//! This module offers `/proc/<pid>/net` file support, which exposes the
//! network statistics visible to one task.
//!
//! This kernel has a single network namespace, so every task sees the same
//! interfaces; the directory exists because standard tools read
//! `/proc/net/dev`, and `/proc/net` is a link to `self/net`.
//!
//! Reference: <https://man7.org/linux/man-pages/man5/proc_pid_net.5.html>

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
    net::iface_statuses,
    prelude::*,
    thread::Thread,
};

/// Represents the inode at `/proc/<pid>/net`.
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

/// Represents the inode at `/proc/<pid>/net/dev`.
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

        for status in iface_statuses() {
            // Only byte and packet counters are instrumented today. The other
            // Linux columns remain zero until their events are tracked.
            let fields = [
                status.stats.rx_bytes,
                status.stats.rx_packets,
                0,
                0,
                0,
                0,
                0,
                0,
                status.stats.tx_bytes,
                status.stats.tx_packets,
                0,
                0,
                0,
                0,
                0,
                0,
            ];
            write!(printer, "{:>6}:", status.name)?;
            for value in fields {
                write!(printer, " {:>8}", value)?;
            }
            writeln!(printer)?;
        }

        Ok(printer.bytes_written())
    }
}
