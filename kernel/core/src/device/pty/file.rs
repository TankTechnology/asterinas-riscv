// SPDX-License-Identifier: MPL-2.0

use inherit_methods_macro::inherit_methods;

use super::driver::PtyDriver;
use crate::{
    device::{PtySlave, tty::TtyDriver},
    events::IoEvents,
    fs::{
        file::{PerOpenFileOps, SettableStatusFlags, StatusFlags},
        vfs::{inode::FileOps, path::Path},
    },
    prelude::*,
    process::signal::{PollHandle, Pollable},
    util::ioctl::RawIoctl,
};

/// The file for a pseudoterminal slave.
pub(super) struct PtySlaveFile(Arc<PtySlave>);

impl PtySlaveFile {
    pub(super) fn new(slave: Arc<PtySlave>) -> Result<PtySlaveFile> {
        // Reference: <https://elixir.bootlin.com/linux/v6.17/source/drivers/tty/pty.c#L220>.

        // Hold the lock at first to avoid race condition.
        let mut opened_slaves = slave.driver().opened_slaves().lock();

        let master_flags = slave.driver().tty_flags();
        // On Linux the slave may still be opened after the master is closed
        // (it just sees a hangup), so only reject while the master has it
        // locked. A Unix98 PTY starts locked and unlockpt(3) clears this flag.
        if master_flags.is_pty_locked() {
            return_errno_with_message!(
                Errno::EIO,
                "a pty slave cannot be opened when the pty master is locked"
            );
        }
        master_flags.clear_other_closed();

        *opened_slaves += 1;
        drop(opened_slaves);

        slave.driver().pollee().invalidate();

        // Linux assigns the controlling terminal automatically on the first
        // read-write open of a slave by a session leader without a controlling
        // terminal -- unless the open carried `O_NOCTTY`. That decision needs
        // the open's status flags, which this constructor does not see, so it
        // is made one layer up, in `PtySlaveInode::open` (via
        // `Terminal::acquire_control_on_open`). Doing it here as well ignored
        // `O_NOCTTY`: sshd's monitor opens the slave with `openpty()`, which
        // sets `O_NOCTTY`, and the early claim stole the pty from the session
        // that was about to run the shell, so the shell's own `TIOCSCTTY`
        // answered `EPERM`.

        Ok(PtySlaveFile(slave))
    }
}

impl Drop for PtySlaveFile {
    fn drop(&mut self) {
        let driver = self.0.driver();

        let mut opened_slaves = driver.opened_slaves().lock();
        *opened_slaves -= 1;

        if *opened_slaves == 0 {
            driver.tty_flags().set_other_closed();
            driver.pollee().notify(IoEvents::HUP);
        }
    }
}

#[inherit_methods(from = "self.0")]
impl Pollable for PtySlaveFile {
    fn poll(&self, mask: IoEvents, poller: Option<&mut PollHandle>) -> IoEvents;
}

impl FileOps for PtySlaveFile {
    fn read_at(
        &self,
        _offset: usize,
        writer: &mut VmWriter,
        status_flags: StatusFlags,
    ) -> Result<usize> {
        self.0.read(writer, status_flags)
    }

    fn write_at(
        &self,
        _offset: usize,
        reader: &mut VmReader,
        status_flags: StatusFlags,
    ) -> Result<usize> {
        self.0.write(reader, status_flags)
    }
}

impl PerOpenFileOps for PtySlaveFile {
    fn is_scm_rights_proven_leaf(&self) -> bool {
        PtyDriver::SCM_RIGHTS_PROVEN_LEAF
    }

    fn check_seekable(&self) -> Result<()> {
        return_errno_with_message!(Errno::ESPIPE, "the inode is a TTY");
    }

    fn is_offset_aware(&self) -> bool {
        false
    }

    fn ioctl(&self, _path: &Path, raw_ioctl: RawIoctl) -> Result<i32> {
        self.0.ioctl(raw_ioctl)
    }

    fn settable_status_flags(&self) -> SettableStatusFlags {
        SettableStatusFlags::minimal().with_o_async()
    }
}
