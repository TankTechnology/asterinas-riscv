// SPDX-License-Identifier: MPL-2.0

use inherit_methods_macro::inherit_methods;

use super::{Tty, TtyDriver};
use crate::{
    events::IoEvents,
    fs::{
        file::{PerOpenFileOps, SettableStatusFlags, StatusFlags},
        vfs::{inode::FileOps, path::Path},
    },
    prelude::*,
    process::signal::{PollHandle, Pollable},
    util::ioctl::RawIoctl,
};

pub(super) struct TtyFile<D>(Arc<Tty<D>>);

impl<D: TtyDriver> TtyFile<D> {
    pub(super) fn new(tty: Arc<Tty<D>>) -> Self {
        Self(tty)
    }
}

#[inherit_methods(from = "self.0")]
impl<D: TtyDriver> Pollable for TtyFile<D> {
    fn poll(&self, mask: IoEvents, poller: Option<&mut PollHandle>) -> IoEvents;
}

impl<D: TtyDriver> FileOps for TtyFile<D> {
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

impl<D: TtyDriver> PerOpenFileOps for TtyFile<D> {
    fn is_scm_rights_proven_leaf(&self) -> bool {
        D::SCM_RIGHTS_PROVEN_LEAF
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

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;
    use crate::device::tty::{hvc::HvcDriver, serial::SerialDriver, vt::VtDriver};

    #[ktest]
    fn scm_rights_leaf_requires_concrete_driver_opt_in() {
        assert!(!VtDriver::SCM_RIGHTS_PROVEN_LEAF);
        assert!(HvcDriver::SCM_RIGHTS_PROVEN_LEAF);
        assert!(SerialDriver::SCM_RIGHTS_PROVEN_LEAF);
    }
}
