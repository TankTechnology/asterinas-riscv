// SPDX-License-Identifier: MPL-2.0

//! Deliberately stop feeding the hardware watchdog at
//! `/proc/sys/kernel/asterinas_watchdog_drill`.
//!
//! The last recovery layer is a DesignWare watchdog that U-Boot arms before
//! the kernel starts. Nothing on a healthy board ever stops the heartbeat, so
//! the one assumption that layer rests on -- *an un-fed watchdog resets this
//! SoC* -- cannot be checked on the hardware by any other means short of
//! physically wedging the board.
//!
//! Writing `1` here stops the heartbeat for the rest of the boot. On a board
//! where the handoff is active the SoC is expected to reset within the
//! watchdog timeout (tens of seconds), which is what makes the drill a
//! pass/fail observation instead of a guess. It is a debug facility with a
//! sharp edge: it only takes effect when the handoff actually happened, it
//! requires `CAP_SYS_ADMIN` to write, and reading the file reports whether the
//! drill has already been fired this boot.

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{self, ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    hardware_watchdog,
    prelude::*,
    process::{UserNamespace, credentials::capabilities::CapSet, posix_thread::AsPosixThread},
    security::lsm::hooks as lsm_hooks,
};

/// The one-way hardware watchdog drill at
/// `/proc/sys/kernel/asterinas_watchdog_drill`.
pub(super) struct AsterinasWatchdogDrillFileOps;

impl AsterinasWatchdogDrillFileOps {
    pub(super) fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r, u+w))
    }
}

impl ProcFileOps for AsterinasWatchdogDrillFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(printer, "{}", u8::from(hardware_watchdog::is_kicking()))?;
        Ok(printer.bytes_written())
    }

    fn write_at(&self, offset: usize, reader: &mut VmReader) -> Result<usize> {
        lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
            UserNamespace::get_init_singleton().as_ref(),
            current_thread!().as_posix_thread().unwrap(),
            CapSet::SYS_ADMIN,
        ))?;
        if offset != 0 {
            return_errno_with_message!(Errno::EINVAL, "sysctl writes must start at offset zero");
        }
        let (value, count) = template::read_i32_from(reader)?;
        if value != 1 {
            return_errno_with_message!(
                Errno::EINVAL,
                "asterinas_watchdog_drill accepts only the fire value one"
            );
        }
        hardware_watchdog::stop_kicking_for_reset();
        Ok(count)
    }
}
