// SPDX-License-Identifier: MPL-2.0

//! `/proc/perm_order`: which order `check_permission` uses.
//!
//! Measurement scaffolding. Both orders answer identically; they differ in
//! how much work a permission the mode bits already grant costs. Being able
//! to flip between them inside one boot is what makes their cost comparable,
//! because a perf counter measured across two boots moves with cache and
//! frequency state as much as with the code.
//!
//! ```text
//! echo legacy > /proc/perm_order   # capability check first (old order)
//! echo fast   > /proc/perm_order   # mode bits first (default)
//! cat /proc/perm_order
//! ```

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::{self, Inode},
    },
    prelude::*,
};

/// Represents the inode at `/proc/perm_order`.
pub struct PermOrderFileOps;

impl PermOrderFileOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+rw))
    }
}

impl ProcFileOps for PermOrderFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        let order = if inode::legacy_permission_order() {
            "legacy\n"
        } else {
            "fast\n"
        };
        write!(printer, "{}", order)?;
        Ok(printer.bytes_written())
    }

    fn write_at(&self, _offset: usize, reader: &mut VmReader) -> Result<usize> {
        let mut buffer = [0u8; 16];
        let read = reader
            .read_fallible(&mut VmWriter::from(&mut buffer[..]))
            .map_err(|(err, _)| err)?;
        let text = core::str::from_utf8(&buffer[..read]).unwrap_or("");
        match text.trim() {
            "legacy" | "1" => inode::set_legacy_permission_order(true),
            "fast" | "0" => inode::set_legacy_permission_order(false),
            _ => return_errno_with_message!(Errno::EINVAL, "expected `fast` or `legacy`"),
        }
        Ok(read)
    }
}
