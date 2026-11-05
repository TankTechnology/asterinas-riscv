// SPDX-License-Identifier: MPL-2.0

//! `/proc/sysrq-trigger`: the one power command that still works when the
//! filesystem does not.
//!
//! A board whose root filesystem has gone bad still has a shell, but that
//! shell cannot start a new program: `reboot`, `systemctl` and even `sync` are
//! read from the broken disk and fail with `Exec format error`. Writing a
//! character to this file needs no executable and no filesystem, so it is the
//! only way out without hands on the board.
//!
//! Reference: <https://man7.org/linux/man-pages/man5/proc_sysrq-trigger.5.html>

use ostd::power::{ExitCode, poweroff, restart};

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
    process::{UserNamespace, credentials::capabilities::CapSet, posix_thread::AsPosixThread},
    security::lsm::hooks as lsm_hooks,
};

/// Represents the inode at `/proc/sysrq-trigger`.
pub struct SysRqTriggerFileOps;

impl SysRqTriggerFileOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Write-only: the trigger takes a command, it does not report state.
        ProcFile::new(Self, parent, mkmod!(u+w))
    }
}

impl ProcFileOps for SysRqTriggerFileOps {
    fn read_at(&self, _offset: usize, _writer: &mut VmWriter) -> Result<usize> {
        return_errno_with_message!(Errno::EINVAL, "sysrq-trigger is write-only");
    }

    fn write_at(&self, offset: usize, reader: &mut VmReader) -> Result<usize> {
        lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
            UserNamespace::get_init_singleton().as_ref(),
            current_thread!().as_posix_thread().unwrap(),
            CapSet::SYS_BOOT,
        ))?;
        if offset != 0 {
            return_errno_with_message!(Errno::EINVAL, "sysrq-trigger takes one command");
        }

        let mut command = [0u8; 8];
        let count = reader.read_fallible(&mut VmWriter::from(&mut command[..]))?;
        let text = core::str::from_utf8(&command[..count])
            .map_err(|_| Error::with_message(Errno::EINVAL, "the command is not text"))?;
        let Some(key) = text.trim().chars().next() else {
            return_errno_with_message!(Errno::EINVAL, "no command was given");
        };
        if text.trim().chars().count() != 1 {
            return_errno_with_message!(Errno::EINVAL, "sysrq-trigger takes exactly one character");
        }

        // Deliberately no `sync`: a caller that reaches for this has a
        // filesystem that cannot be written reliably, and a flush that blocks
        // or fails is what leaves a board stuck. Data that fits belongs with
        // `sync` and `reboot(2)`; this is the emergency exit.
        match key {
            'b' => restart(ExitCode::Success),
            'o' | 'h' => poweroff(ExitCode::Success),
            _ => return_errno_with_message!(
                Errno::EINVAL,
                "supported commands are 'b' (reboot), 'o' (power off) and 'h' (halt)"
            ),
        }
    }
}
