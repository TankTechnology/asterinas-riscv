// SPDX-License-Identifier: MPL-2.0

use aster_logger::klog;
use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{self, ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
    process::{UserNamespace, credentials::capabilities::CapSet, posix_thread::AsPosixThread},
    security::lsm::hooks as lsm_hooks,
};

/// The kernel log access policy at `/proc/sys/kernel/dmesg_restrict`.
pub(super) struct DmesgRestrictFileOps;

impl DmesgRestrictFileOps {
    pub(super) fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r, u+w))
    }
}

impl ProcFileOps for DmesgRestrictFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(printer, "{}", u8::from(klog::klog().dmesg_restrict()))?;
        Ok(printer.bytes_written())
    }

    fn write_at(&self, _offset: usize, reader: &mut VmReader) -> Result<usize> {
        lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
            UserNamespace::get_init_singleton().as_ref(),
            current_thread!().as_posix_thread().unwrap(),
            CapSet::SYS_ADMIN,
        ))?;
        let (value, count) = template::read_i32_from(reader)?;
        if !matches!(value, 0 | 1) {
            return_errno_with_message!(Errno::EINVAL, "dmesg_restrict must be zero or one");
        }
        klog::klog().set_dmesg_restrict(value == 1);
        Ok(count)
    }
}
