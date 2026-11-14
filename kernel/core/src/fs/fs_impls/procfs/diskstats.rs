// SPDX-License-Identifier: MPL-2.0

//! This module offers `/proc/diskstats`, the per-device I/O counter table that
//! `iostat`, `vmstat -d`, `sar`, and most monitoring agents read before they
//! can report anything at all.
//!
//! Reference: <https://docs.kernel.org/admin-guide/iostats.html>
//!
//! The column layout is fixed and tools disagree about how to recover from a
//! missing one, so every line carries all fourteen fields even when a counter
//! has never been incremented. The block layer counts completed requests and
//! the sectors they moved; the timing columns stay zero because the kernel does
//! not timestamp request service, and a zero is an answer where a missing
//! column is a parse error.

use alloc::string::String;

use aster_block::BlockDevice;
use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
};

/// Represents the inode at `/proc/diskstats`.
pub struct DiskStatsFileOps;

impl DiskStatsFileOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for DiskStatsFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);

        let devices = aster_block::collect_all();

        // A device a parent lists as its partition is printed once, after that
        // parent: the registry hands partitions out as devices of their own and
        // `is_partition()` is not implemented by every driver, so the name is
        // the reliable signal here too.
        let mut partition_names: Vec<String> = Vec::new();
        for device in &devices {
            for partition in device.partitions().unwrap_or_default() {
                partition_names.push(String::from(partition.name()));
            }
        }

        for device in &devices {
            if partition_names.iter().any(|name| name == device.name()) {
                continue;
            }
            print_device(&mut printer, device.as_ref())?;
            for partition in device.partitions().unwrap_or_default() {
                print_device(&mut printer, partition.as_ref())?;
            }
        }

        Ok(printer.bytes_written())
    }
}

fn print_device(printer: &mut VmPrinter, device: &dyn BlockDevice) -> Result<()> {
    let id = device.id();
    let name = device.name();
    let stats = aster_block::iostats::lookup(id)
        .map(|stats| stats.snapshot())
        .unwrap_or_default();

    // major minor name, then the fourteen fields iostat parses. Merges and the
    // three timing columns have no source here and read as zero.
    writeln!(
        printer,
        "{} {} {} {} 0 {} 0 {} 0 {} 0 {} 0 0 0",
        id.major().get(),
        id.minor().get(),
        name,
        stats.reads,
        stats.read_sectors,
        stats.writes,
        stats.write_sectors,
        stats.in_flight
    )?;
    Ok(())
}
