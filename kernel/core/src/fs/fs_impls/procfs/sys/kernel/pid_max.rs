// SPDX-License-Identifier: MPL-2.0

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{self, ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
    process::posix_thread::{PID_MAX, pid_max, set_pid_max},
};

/// Validates a raw `pid_max` value against the same range that
/// [`set_pid_max`] enforces, so the procfs write path rejects values before
/// touching the allocator.
pub(super) fn validate_pid_max(value: u64) -> Result<u32> {
    let value = u32::try_from(value)
        .map_err(|_| Error::with_message(Errno::EINVAL, "pid_max is out of range"))?;
    if !(301..=PID_MAX).contains(&value) {
        return_errno_with_message!(Errno::EINVAL, "pid_max is out of range");
    }
    Ok(value)
}

/// Represents the inode at `/proc/sys/kernel/pid_max`.
pub(super) struct PidMaxFileOps;

impl PidMaxFileOps {
    pub(super) fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference: <https://elixir.bootlin.com/linux/v6.16.5/source/kernel/pid.c#L721>
        ProcFile::new(Self, parent, mkmod!(a+r, u+w))
    }
}

impl ProcFileOps for PidMaxFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);

        writeln!(printer, "{}", pid_max())?;

        Ok(printer.bytes_written())
    }

    fn write_at(&self, offset: usize, reader: &mut VmReader) -> Result<usize> {
        if offset != 0 {
            return_errno_with_message!(Errno::EINVAL, "sysctl writes must start at offset zero");
        }

        let (value, count) = template::read_i32_from(reader)?;
        let value = validate_pid_max(value as u64)?;
        set_pid_max(value)?;
        Ok(count)
    }
}
