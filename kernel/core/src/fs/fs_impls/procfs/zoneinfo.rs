// SPDX-License-Identifier: MPL-2.0

//! `/proc/zoneinfo`, the per-zone memory state that `vmstat`, `sar` and most
//! monitoring agents open next to `/proc/meminfo`.
//!
//! Reference: <https://man7.org/linux/man-pages/man5/proc_zoneinfo.5.html>
//!
//! There is one pool of physical memory and no reclaim, so there is one zone.
//! Its watermark fields are zero because nothing compares against them; the
//! numbers that tools actually plot -- `pages free`, `spanned`, `present`,
//! `managed` -- are real.

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
};

const PAGE_SIZE: usize = 4096;

/// Represents the inode at `/proc/zoneinfo`.
pub struct ZoneInfoFileOps;

impl ZoneInfoFileOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for ZoneInfoFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);

        let free_bytes = osdk_frame_allocator::load_total_free_size();
        let total_pages = crate::vm::mem_total() / PAGE_SIZE;
        let free_pages = free_bytes / PAGE_SIZE;

        writeln!(printer, "Node 0, zone   Normal")?;
        writeln!(printer, "  pages free     {}", free_pages)?;
        writeln!(printer, "        min      0")?;
        writeln!(printer, "        low      0")?;
        writeln!(printer, "        high     0")?;
        writeln!(printer, "        spanned  {}", total_pages)?;
        writeln!(printer, "        present  {}", total_pages)?;
        writeln!(printer, "        managed  {}", total_pages)?;
        writeln!(printer, "        cma      0")?;
        writeln!(printer, "  nr_free_pages {}", free_pages)?;
        writeln!(printer, "  nr_zone_inactive_anon 0")?;
        writeln!(printer, "  nr_zone_active_anon 0")?;
        writeln!(printer, "  nr_zone_inactive_file 0")?;
        writeln!(printer, "  nr_zone_active_file 0")?;
        writeln!(printer, "  nr_zone_unevictable 0")?;
        writeln!(printer, "  nr_zone_write_pending 0")?;
        // Nothing in this kernel reclaims, so every threshold report is zero
        // and there is no per-cpu page set to describe.
        writeln!(printer, "  nr_mlock 0")?;
        writeln!(printer, "  nr_page_table_pages 0")?;
        writeln!(printer, "  nr_kernel_stack 0")?;
        writeln!(printer, "  pgprot_nx 0")?;
        writeln!(printer, "  node_unreclaimable:  0")?;
        writeln!(printer, "  start_pfn:           0")?;

        Ok(printer.bytes_written())
    }
}
