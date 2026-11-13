// SPDX-License-Identifier: MPL-2.0

//! This module offers `/proc/partitions` support, the table `lsblk`, `fdisk`,
//! `df`'s older paths and partitioning scripts read to enumerate block devices
//! without walking sysfs.
//!
//! Reference: <https://man7.org/linux/man-pages/man5/proc_partitions.5.html>

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

const SECTORS_PER_KIB: usize = 2;

/// Represents the inode at `/proc/partitions`.
pub struct PartitionsFileOps;

impl PartitionsFileOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for PartitionsFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);

        // Linux prints this header with the same alignment.
        writeln!(printer, "major minor  #blocks  name")?;

        for device in aster_block::collect_all() {
            print_device(&mut printer, device.as_ref())?;
            // A partition is listed after its whole device, and the whole
            // device's entry comes first, as in Linux.
            for partition in device.partitions().unwrap_or_default() {
                print_device(&mut printer, partition.as_ref())?;
            }
        }

        Ok(printer.bytes_written())
    }
}

fn print_device(printer: &mut VmPrinter, device: &dyn BlockDevice) -> Result<()> {
    let id = device.id();
    // The kernel reports the size in 1024-byte blocks regardless of the
    // device's own sector size.
    let kib = device.metadata().nr_sectors / SECTORS_PER_KIB;
    writeln!(
        printer,
        "{:>5} {:>5} {:>9} {}",
        id.major().get(),
        id.minor().get(),
        kib,
        device.name()
    )?;
    Ok(())
}
