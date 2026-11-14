// SPDX-License-Identifier: MPL-2.0

//! `/proc/buddyinfo`, the free-block histogram per node and zone.
//!
//! Reference: <https://man7.org/linux/man-pages/man5/proc_buddyinfo.5.html>
//!
//! The kernel has one physical memory pool and no NUMA nodes, so the file has
//! one line, and it reports every buddy order the allocator tracks rather than
//! Linux's usual eleven: a chunk of order 20 is real memory and hiding it would
//! make the numbers stop adding up.

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
};

/// Represents the inode at `/proc/buddyinfo`.
pub struct BuddyInfoFileOps;

impl BuddyInfoFileOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for BuddyInfoFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        let orders = osdk_frame_allocator::load_free_chunks_by_order();
        write!(printer, "Node 0, zone   Normal")?;
        for count in orders {
            write!(printer, " {}", count)?;
        }
        writeln!(printer)?;
        Ok(printer.bytes_written())
    }
}
