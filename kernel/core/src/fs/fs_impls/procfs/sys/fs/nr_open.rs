// SPDX-License-Identifier: MPL-2.0

use core::sync::atomic::Ordering;

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

/// Represents the inode at `/proc/sys/fs/nr_open`.
pub(super) struct NrOpenFileOps;

impl NrOpenFileOps {
    pub(super) fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference: <https://elixir.bootlin.com/linux/v6.16.5/source/fs/file_table.c#L130>
        ProcFile::new(Self, parent, mkmod!(a+r, u+w))
    }
}

impl ProcFileOps for NrOpenFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);

        writeln!(printer, "{}", SYSCTL_NR_OPEN.load(Ordering::Relaxed))?;

        Ok(printer.bytes_written())
    }

    fn write_at(&self, offset: usize, reader: &mut VmReader) -> Result<usize> {
        // Linux parses the whole value with `proc_dointvec_minmax`: a write at
        // a non-zero offset is not a partial update.
        if offset != 0 {
            return_errno_with_message!(Errno::EINVAL, "nr_open must be written from offset 0");
        }

        let len = reader.remain();
        let mut buf = [0u8; 32];
        let copy_len = len.min(buf.len());
        let mut writer = VmWriter::from(&mut buf[..copy_len]).to_fallible();
        let copied = reader.read_fallible(&mut writer)?;
        reader.skip(len - copied);

        let text = core::str::from_utf8(&buf[..copied])
            .map_err(|_| Error::with_message(Errno::EINVAL, "nr_open is not ASCII"))?
            .trim();
        let value: u64 = text
            .parse()
            .map_err(|_| Error::with_message(Errno::EINVAL, "invalid nr_open value"))?;
        if value == 0 {
            return_errno_with_message!(Errno::EINVAL, "nr_open must be positive");
        }

        SYSCTL_NR_OPEN.store(value, Ordering::Relaxed);
        Ok(len)
    }
}
