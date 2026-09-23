// SPDX-License-Identifier: MPL-2.0

//! The network statistics visible to one task's network namespace.

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
    net::net_ns::NetNamespace,
    prelude::*,
    process::posix_thread::AsPosixThread,
    thread::Thread,
};

/// The `/proc/[pid]/net` directory, also inherited by `/proc/[pid]/task/[tid]`.
pub(super) struct NetDirOps(TidDirOps);

impl NetDirOps {
    pub(super) fn new_inode(dir: &TidDirOps, parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcDir::new(Self(dir.clone()), parent, mkmod!(a+rx))
    }

    fn net_ns(&self) -> Result<Arc<NetNamespace>> {
        let thread = self
            .0
            .thread()
            .ok_or_else(|| Error::with_message(Errno::ESRCH, "the thread does not exist"))?;
        let ns_proxy = thread.as_posix_thread().unwrap().ns_proxy().lock();
        let proxy = ns_proxy
            .as_ref()
            .ok_or_else(|| Error::with_message(Errno::ENOENT, "the thread has exited"))?;
        Ok(proxy.net_ns().clone())
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
        Ok(DevFileOps::new_inode(
            self.net_ns()?,
            this_dir.this_weak().clone(),
        ))
    }

    fn visit_entries_from_offset<'a, F>(&'a self, offset: usize, visit_fn: F) -> Result<()>
    where
        F: FnMut(ReaddirEntry<'a>) -> Result<()>,
    {
        self.net_ns()?;
        visit_listed_entries(offset, [ListedEntry::new("dev", InodeType::File)], visit_fn)
    }
}

struct DevFileOps(Arc<NetNamespace>);

impl DevFileOps {
    fn new_inode(net_ns: Arc<NetNamespace>, parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self(net_ns), parent, mkmod!(a+r))
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

        for iface in self.0.ifaces() {
            let stats = iface.stats();
            // Only packet and byte counters are instrumented today. The other
            // Linux columns remain zero until their corresponding events are tracked.
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
            write!(printer, "{:>6}:", iface.name().to_str().unwrap())?;
            for value in fields {
                write!(printer, " {:>8}", value)?;
            }
            writeln!(printer)?;
        }

        Ok(printer.bytes_written())
    }
}
