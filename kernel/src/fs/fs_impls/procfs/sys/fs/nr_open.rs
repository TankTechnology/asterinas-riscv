// SPDX-License-Identifier: MPL-2.0

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps, read_u64_from},
        vfs::inode::Inode,
    },
    prelude::*,
    process::rlimit::{set_sysctl_nr_open, sysctl_nr_open},
};

/// Represents the inode at `/proc/sys/fs/nr_open`.
pub struct NrOpenFileOps;

impl NrOpenFileOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference: <https://elixir.bootlin.com/linux/v6.16.5/source/fs/file_table.c#L130>
        ProcFile::new(Self, parent, mkmod!(a+r, u+w))
    }
}

impl ProcFileOps for NrOpenFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);

        writeln!(printer, "{}", sysctl_nr_open())?;

        Ok(printer.bytes_written())
    }

    fn write_at(&self, _offset: usize, reader: &mut VmReader) -> Result<usize> {
        let (value, read_bytes) = read_u64_from(reader)?;
        set_sysctl_nr_open(value)?;
        Ok(read_bytes)
    }
}
