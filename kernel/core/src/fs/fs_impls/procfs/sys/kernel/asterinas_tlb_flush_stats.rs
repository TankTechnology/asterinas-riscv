// SPDX-License-Identifier: MPL-2.0

//! `/proc/sys/kernel/asterinas_tlb_flush_stats`: which code path flushes.
//!
//! The board resets under a burst of inter-processor TLB flushes, and the
//! IPI counters in the watchdog heartbeat say how many were sent but not who
//! sent them. This file reports the per-site counters plus the IPI totals on
//! demand, so a QEMU run can answer the same question without a board, and a
//! board log can be taken at a known moment rather than only on a heartbeat.
//!
//! It is read-only diagnostics: nothing about the kernel's behaviour depends
//! on it being read.

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
    vm::vmar::flush_stats,
};

/// The read-only TLB flush statistics file at
/// `/proc/sys/kernel/asterinas_tlb_flush_stats`.
pub(super) struct AsterinasTlbFlushStatsFileOps;

impl AsterinasTlbFlushStatsFileOps {
    pub(super) fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for AsterinasTlbFlushStatsFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        let (received, sent, waits) = ostd::smp::ipi_counters();
        let (tx, rx) = ostd::smp::ipi_kind_counters();
        let sites = flush_stats::snapshot();

        writeln!(
            printer,
            "no_remote_flush={}",
            u8::from(ostd::mm::tlb::no_remote_flush())
        )?;
        writeln!(
            printer,
            "ipi_rx={} ipi_tx={} ipi_wait={} tx_flush={} tx_preempt={} tx_memb={} tx_other={} \
rx_flush={} rx_preempt={} rx_memb={} rx_other={}",
            received, sent, waits, tx[0], tx[1], tx[2], tx[3], rx[0], rx[1], rx[2], rx[3]
        )?;
        writeln!(
            printer,
            "flush_site: ad={} cow_shared={} fresh={} unmap={} protect={} freeze={} remap={} \
fork={} cow_copy={}",
            sites[0],
            sites[1],
            sites[2],
            sites[3],
            sites[4],
            sites[5],
            sites[6],
            sites[7],
            sites[8]
        )?;
        // How many mappings one range operation covers decides whether batching
        // its flushes can pay at all; the maximum hold time decides whether a
        // whole-range batch is acceptable.
        let (munmap, mprotect) = flush_stats::range_op_shapes();
        writeln!(
            printer,
            "op_shape: munmap_calls={} munmap_maps={} munmap_max_maps={} munmap_max_us={} \
mprotect_calls={} mprotect_maps={} mprotect_max_maps={} mprotect_max_us={}",
            munmap.calls,
            munmap.mappings,
            munmap.max_mappings,
            munmap.max_micros,
            mprotect.calls,
            mprotect.mappings,
            mprotect.max_mappings,
            mprotect.max_micros
        )?;
        // The kernel-side sites can only cover the sites someone instrumented.
        // These count the dispatches themselves, so whatever is left after
        // subtracting the named sites is a real, unnamed source rather than
        // noise.
        let (dispatches, with_remote, remote_targets) = ostd::mm::tlb::dispatch_counters();
        let (cursor_unmap, dma_map) = ostd::mm::tlb::origin_counters();
        writeln!(
            printer,
            "dispatch: total={} with_remote={} remote_targets={} cursor_unmap={} dma_map={}",
            dispatches, with_remote, remote_targets, cursor_unmap, dma_map
        )?;
        Ok(printer.bytes_written())
    }
}
