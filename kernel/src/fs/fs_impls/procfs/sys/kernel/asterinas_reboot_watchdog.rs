// SPDX-License-Identifier: MPL-2.0

use aster_util::printer::VmPrinter;

use crate::{
    boot_reboot,
    fs::{
        file::mkmod,
        procfs::template::{self, ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
    process::{UserNamespace, credentials::capabilities::CapSet, posix_thread::AsPosixThread},
    security::lsm::hooks as lsm_hooks,
};

/// The one-way boot recovery watchdog control at
/// `/proc/sys/kernel/asterinas_reboot_watchdog`.
pub(super) struct AsterinasRebootWatchdogFileOps;

impl AsterinasRebootWatchdogFileOps {
    pub(super) fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r, u+w))
    }
}

pub(super) fn validate_write_offset(offset: usize) -> Result<()> {
    if offset != 0 {
        return_errno_with_message!(Errno::EINVAL, "sysctl writes must start at offset zero");
    }
    Ok(())
}

pub(super) fn validate_disarm_value(value: i32) -> Result<()> {
    if value != 0 {
        return_errno_with_message!(
            Errno::EINVAL,
            "asterinas_reboot_watchdog accepts only the disarm value zero"
        );
    }
    Ok(())
}

impl ProcFileOps for AsterinasRebootWatchdogFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(printer, "{}", u8::from(boot_reboot::is_armed()))?;
        Ok(printer.bytes_written())
    }

    fn write_at(&self, offset: usize, reader: &mut VmReader) -> Result<usize> {
        lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
            UserNamespace::get_init_singleton().as_ref(),
            current_thread!().as_posix_thread().unwrap(),
            CapSet::SYS_ADMIN,
        ))?;
        validate_write_offset(offset)?;
        let (value, count) = template::read_i32_from(reader)?;
        validate_disarm_value(value)?;
        boot_reboot::disarm();
        Ok(count)
    }
}
