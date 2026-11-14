// SPDX-License-Identifier: MPL-2.0

//! `/proc/pagetypeinfo`, the same buddy histogram as `/proc/buddyinfo` split by
//! page migration type.
//!
//! Reference: <https://man7.org/linux/man-pages/man5/proc_pagetypeinfo.5.html>
//!
//! This kernel does not tag free chunks with a migration type, so every chunk
//! is reported as `Unmovable` and the other types are zero. The shape is what
//! the readers parse; the total across a row still equals the buddy count.

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
};

/// Represents the inode at `/proc/pagetypeinfo`.
pub struct PageTypeInfoFileOps;

impl PageTypeInfoFileOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for PageTypeInfoFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        let orders = osdk_frame_allocator::load_free_chunks_by_order();

        // Linux prints the order of the page-block bitmap; there is none here,
        // so report the largest tracked order and the pages it covers.
        let page_block_order = osdk_frame_allocator::BUDDY_ORDERS - 1;
        writeln!(printer, "Page block order: {}", page_block_order)?;
        writeln!(printer, "Pages per block:  {}", 1usize << page_block_order)?;
        writeln!(printer)?;

        write!(printer, "Free pages count per migrate type at order")?;
        for order in 0..orders.len() {
            write!(printer, " {:>6}", order)?;
        }
        writeln!(printer)?;

        // Every chunk is unmovable; the other types exist so readers see a
        // full table rather than a short one.
        for (index, migrate_type) in [
            "Unmovable",
            "Movable",
            "Reclaimable",
            "HighAtomic",
            "CMA",
            "Isolate",
        ]
        .iter()
        .enumerate()
        {
            write!(printer, "Node    0, zone   Normal, type {:>12}", migrate_type)?;
            for count in &orders {
                write!(printer, " {:>6}", if index == 0 { *count } else { 0 })?;
            }
            writeln!(printer)?;
        }

        writeln!(printer)?;
        writeln!(printer, "Number of blocks type     Unmovable   Movable  Reclaimable   HighAtomic         CMA     Isolate")?;
        writeln!(printer, "Node    0, zone   Normal           0         0            0            0           0           0")?;

        Ok(printer.bytes_written())
    }
}
