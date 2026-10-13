// SPDX-License-Identifier: MPL-2.0

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
};

/// Represents the inode at `/proc/sys/net/ipv4/ip_local_reserved_ports`.
pub(super) struct ReservedPortsFileOps;

impl ReservedPortsFileOps {
    pub(super) fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference:
        // <https://elixir.bootlin.com/linux/v6.16.5/source/net/ipv4/sysctl_net_ipv4.c>
        //
        // The port allocator has no configurable reservations. Report its
        // actual empty reservation set, but do not pretend to support writes.
        // libtirpc reads this sysctl even when no ports have been reserved.
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for ReservedPortsFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);

        writeln!(printer)?;

        Ok(printer.bytes_written())
    }
}
