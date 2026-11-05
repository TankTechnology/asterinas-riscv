// SPDX-License-Identifier: MPL-2.0

use alloc::borrow::ToOwned;

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
};

static CORE_PATTERN: spin::Mutex<String> = spin::Mutex::new(String::new());

pub(super) fn validate_core_pattern(value: &str) -> Result<()> {
    if value.is_empty() || value.as_bytes().contains(&0) {
        return_errno_with_message!(Errno::EINVAL, "core_pattern must not be empty");
    }
    Ok(())
}

pub(super) struct CorePatternFileOps;

impl CorePatternFileOps {
    pub(super) fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r, u+w))
    }
}

impl ProcFileOps for CorePatternFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let pattern = CORE_PATTERN.lock();
        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(
            printer,
            "{}",
            if pattern.is_empty() { "core" } else { &pattern }
        )?;
        Ok(printer.bytes_written())
    }

    fn write_at(&self, offset: usize, reader: &mut VmReader) -> Result<usize> {
        if offset != 0 {
            return_errno_with_message!(Errno::EINVAL, "sysctl writes must start at offset zero");
        }
        let (value, count) = reader.read_cstring_until_end(128)?;
        let value = value
            .to_str()
            .map_err(|_| Error::with_message(Errno::EINVAL, "core_pattern is not UTF-8"))?
            .trim();
        validate_core_pattern(value)?;
        *CORE_PATTERN.lock() = value.to_owned();
        Ok(count)
    }
}
