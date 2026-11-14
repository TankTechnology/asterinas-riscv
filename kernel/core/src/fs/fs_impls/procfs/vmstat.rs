// SPDX-License-Identifier: MPL-2.0

//! `/proc/vmstat`, the key/value table `vmstat`, `sar` and most monitoring
//! agents read before they will report anything.
//!
//! Reference: <https://docs.kernel.org/admin-guide/sysctl/vm.html>
//!
//! Every key is emitted even when the kernel has no counter behind it: `vmstat`
//! refuses to run at all if the file is missing, and tools treat an absent key
//! and a zero differently. The keys with real values are called out below;
//! everything else is a constant zero, which is the truthful answer for a
//! kernel that does not count that event.

use aster_block::BlockDevice;
use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
    process::global_page_faults,
};

const PAGE_SIZE: usize = 4096;
/// One page is eight 512-byte sectors.
const SECTORS_PER_PAGE: u64 = 8;

/// Represents the inode at `/proc/vmstat`.
pub struct VmStatFileOps;

impl VmStatFileOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for VmStatFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);

        let free_pages = osdk_frame_allocator::load_total_free_size() / PAGE_SIZE;
        let (min_flt, maj_flt) = global_page_faults();
        let (pages_in, pages_out) = block_pages();
        let orders = osdk_frame_allocator::load_free_chunks_by_order();
        let free_chunks: usize = orders.iter().sum();

        // Memory.
        writeln!(printer, "nr_free_pages {}", free_pages)?;
        writeln!(printer, "nr_free_cma 0")?;
        writeln!(printer, "nr_slab_reclaimable 0")?;
        writeln!(printer, "nr_slab_unreclaimable 0")?;
        writeln!(printer, "nr_dirty 0")?;
        writeln!(printer, "nr_writeback 0")?;
        writeln!(printer, "nr_page_table_pages 0")?;
        // Free chunks exactly as `/proc/buddyinfo` counts them; where the two
        // differ from `nr_free_pages` a chunk is larger than one page.
        writeln!(printer, "nr_free_chunks {}", free_chunks)?;

        // Paging, from the block layer's own counters.
        writeln!(printer, "pgpgin {}", pages_in)?;
        writeln!(printer, "pgpgout {}", pages_out)?;
        writeln!(printer, "pswpin 0")?;
        writeln!(printer, "pswpout 0")?;
        writeln!(printer, "pgfault {}", min_flt.saturating_add(maj_flt))?;
        writeln!(printer, "pgmajfault {}", maj_flt)?;

        // Allocation activity. The allocator counts free pages and chunks, not
        // each transition, so these read as zero.
        for key in [
            "pgalloc_dma",
            "pgalloc_dma32",
            "pgalloc_normal",
            "pgfree",
            "pgactivate",
            "pgdeactivate",
            "pgrefill",
            "pgscan_kswapd",
            "pgscan_direct",
            "pgsteal_kswapd",
            "pgsteal_direct",
            "pgrotated",
        ] {
            writeln!(printer, "{} 0", key)?;
        }

        // Reclaim activity: there is no page reclaim in this kernel.
        for key in [
            "allocstall_dma",
            "allocstall_normal",
            "compact_stall",
            "compact_fail",
            "compact_success",
            "kswapd_inodesteal",
        ] {
            writeln!(printer, "{} 0", key)?;
        }

        Ok(printer.bytes_written())
    }
}

/// Pages read from and written to block devices since boot.
///
/// Linux counts pages that went through the page cache; this kernel counts
/// every block request, which is a superset. Both are "pages moved to or from
/// storage", which is what the summary columns mean.
fn block_pages() -> (u64, u64) {
    let mut read_sectors = 0u64;
    let mut write_sectors = 0u64;
    for device in aster_block::collect_all() {
        if let Some(stats) = aster_block::iostats::lookup(device.id()) {
            let snapshot = stats.snapshot();
            read_sectors += snapshot.read_sectors;
            write_sectors += snapshot.write_sectors;
        }
    }
    (
        read_sectors / SECTORS_PER_PAGE,
        write_sectors / SECTORS_PER_PAGE,
    )
}
