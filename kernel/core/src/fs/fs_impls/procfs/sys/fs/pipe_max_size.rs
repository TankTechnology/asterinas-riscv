// SPDX-License-Identifier: MPL-2.0

use aster_util::printer::VmPrinter;
use ostd::mm::VmIo;

use crate::{
    fs::{
        file::mkmod,
        pipe::{PIPE_MAX_SIZE, set_pipe_max_size},
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
};

/// Represents the inode at `/proc/sys/fs/pipe-max-size`.
pub struct PipeMaxSizeFileOps;

impl PipeMaxSizeFileOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference:
        // <https://elixir.bootlin.com/linux/v6.16.5/source/fs/pipe.c#L1366>
        ProcFile::new(Self, parent, mkmod!(a+r, u+w))
    }
}

impl ProcFileOps for PipeMaxSizeFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);

        writeln!(printer, "{}", PIPE_MAX_SIZE.load(core::sync::atomic::Ordering::Relaxed))?;

        Ok(printer.bytes_written())
    }

    fn write_at(&self, _offset: usize, reader: &mut VmReader) -> Result<usize> {
        let mut buf = [0u8; 32];
        let len = reader.remain().min(buf.len());
        let mut writer = VmWriter::from(buf.as_mut_slice()).to_fallible();
        let len = reader
            .read_fallible(&mut writer)
            .map_err(|(err, _)| err)?
            .min(len);
        let text = core::str::from_utf8(&buf[..len]).map_err(|_| {
            Error::with_message(Errno::EINVAL, "the value is not a valid integer")
        })?;
        let value: usize = text.trim().parse().map_err(|_| {
            Error::with_message(Errno::EINVAL, "the value is not a valid integer")
        })?;

        // Linux clamps the minimum to the page size; a smaller value would
        // make a pipe buffer unable to hold even one byte in practice.
        set_pipe_max_size(value.max(4096))?;

        Ok(len)
    }
}
