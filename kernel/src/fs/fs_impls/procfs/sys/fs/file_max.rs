// SPDX-License-Identifier: MPL-2.0

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
    process::rlimit::SYSCTL_NR_OPEN,
};

/// Represents the inode at `/proc/sys/fs/file-max`.
pub struct FileMaxFileOps;

impl FileMaxFileOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference: <https://elixir.bootlin.com/linux/v6.16.5/source/fs/file_table.c#L139>
        ProcFile::new(Self, parent, mkmod!(a+r, u+w))
    }
}

impl ProcFileOps for FileMaxFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);

        writeln!(printer, "{}", SYSCTL_NR_OPEN)?;

        Ok(printer.bytes_written())
    }

    fn write_at(&self, _offset: usize, reader: &mut VmReader) -> Result<usize> {
        // Silently accept the value: systemd bumps this limit at startup and
        // the guest doesn't enforce a real file-max. Report the full length
        // as written — returning 0 makes the caller retry the write forever.
        let len = reader.remain();
        reader.skip(len);
        Ok(len)
    }
}
