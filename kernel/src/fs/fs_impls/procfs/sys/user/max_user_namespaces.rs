// SPDX-License-Identifier: MPL-2.0

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
};

/// Represents the inode at `/proc/sys/user/max_user_namespaces`.
///
/// User namespace creation is always allowed (there is no limit knob), so
/// the file reports the Linux default. Tools such as nix probe this file to
/// decide whether user namespaces are enabled
/// (`userNamespacesSupported()`).
pub struct MaxUserNamespacesFileOps;

/// The Linux default value for `user.max_user_namespaces`.
const MAX_USER_NAMESPACES: u32 = 15000;

impl MaxUserNamespacesFileOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference: <https://elixir.bootlin.com/linux/v6.16.5/source/kernel/ucount.c#L95>
        ProcFile::new(Self, parent, mkmod!(a+r, u+w))
    }
}

impl ProcFileOps for MaxUserNamespacesFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);

        writeln!(printer, "{}", MAX_USER_NAMESPACES)?;

        Ok(printer.bytes_written())
    }

    fn write_at(&self, _offset: usize, _reader: &mut VmReader) -> Result<usize> {
        warn!("writing to `/proc/sys/user/max_user_namespaces` is not supported");
        return_errno_with_message!(
            Errno::EOPNOTSUPP,
            "writing to `/proc/sys/user/max_user_namespaces` is not supported"
        );
    }
}
