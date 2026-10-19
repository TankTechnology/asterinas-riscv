// SPDX-License-Identifier: MPL-2.0

//! `/proc/sys/fs/file-max` — the system-wide open-file limit.
//!
//! The value is exposed and tunable like Linux's, but the kernel does not yet
//! enforce it: a system-wide count of open file descriptions is not maintained
//! (see the resource-limit note at the top of `process/rlimit.rs`).

use core::sync::atomic::{AtomicU64, Ordering};

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
};

/// Represents the inode at `/proc/sys/fs/file-max`.
pub(super) struct FileMaxFileOps;

/// Default value, in the range Linux reports for a few gigabytes of RAM.
static FILE_MAX: AtomicU64 = AtomicU64::new(1 << 20);

impl FileMaxFileOps {
    pub(super) fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference:
        // <https://elixir.bootlin.com/linux/v6.16.5/source/fs/file_table.c#L139>
        // <https://elixir.bootlin.com/linux/v6.16.5/source/fs/proc/proc_sysctl.c#L978>
        ProcFile::new(Self, parent, mkmod!(a+r, u+w))
    }
}

impl ProcFileOps for FileMaxFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);

        writeln!(printer, "{}", FILE_MAX.load(Ordering::Relaxed))?;

        Ok(printer.bytes_written())
    }

    fn write_at(&self, offset: usize, reader: &mut VmReader) -> Result<usize> {
        if offset != 0 {
            return_errno_with_message!(Errno::EINVAL, "file-max must be written from offset 0");
        }

        let len = reader.remain();
        let mut buf = [0u8; 32];
        let copy_len = len.min(buf.len());
        let mut writer = VmWriter::from(&mut buf[..copy_len]).to_fallible();
        let copied = reader.read_fallible(&mut writer)?;
        reader.skip(len - copied);

        let text = core::str::from_utf8(&buf[..copied])
            .map_err(|_| Error::with_message(Errno::EINVAL, "file-max is not ASCII"))?
            .trim();
        let value: u64 = text
            .parse()
            .map_err(|_| Error::with_message(Errno::EINVAL, "invalid file-max value"))?;
        if value == 0 {
            return_errno_with_message!(Errno::EINVAL, "file-max must be positive");
        }

        FILE_MAX.store(value, Ordering::Relaxed);
        Ok(len)
    }
}
