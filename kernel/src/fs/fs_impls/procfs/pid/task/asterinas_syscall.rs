// SPDX-License-Identifier: MPL-2.0

use super::TidDirOps;
use crate::{
    events::IoEvents,
    fs::{
        file::{AccessMode, PerOpenFileOps, StatusFlags, mkmod},
        procfs::template::{ProcFile, ProcFileOpsByHandle},
        vfs::inode::{FileOps, Inode},
    },
    prelude::*,
    process::{
        VmarSnapshot,
        posix_thread::{AsPosixThread, alien_access::AlienAccessMode},
        signal::{PollHandle, Pollable},
    },
    syscall::diagnostics,
    thread::Thread,
};

/// A scalar-only snapshot at `/proc/[pid]/task/[tid]/asterinas_syscall`.
pub(super) struct SyscallFileOps(TidDirOps);

impl SyscallFileOps {
    pub(super) fn new_inode(dir: &TidDirOps, parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self(dir.clone()), parent, mkmod!(u+r))
    }
}

impl ProcFileOpsByHandle for SyscallFileOps {
    fn owner_thread(&self) -> Option<Arc<Thread>> {
        self.0.thread()
    }

    fn open(
        &self,
        access_mode: AccessMode,
        _status_flags: StatusFlags,
    ) -> Result<Box<dyn PerOpenFileOps>> {
        if access_mode.is_writable() {
            return_errno_with_message!(Errno::EACCES, "the syscall snapshot is not writable");
        }
        let Some((thread, process)) = self.0.thread_and_process() else {
            return_errno_with_message!(Errno::ESRCH, "the thread does not exist");
        };
        // As with maps/mem, exec applies credentials under the VMAR lock.
        // Lock order: process VMAR -> diagnostic state. Formatting happens
        // after the diagnostic spinlock is released.
        let vmar_guard = process.lock_vmar();
        check_access(&thread)?;
        let vm = vmar_guard.snapshot();
        let posix_thread = thread.as_posix_thread().unwrap();
        let snapshot = posix_thread
            .syscall_diagnostics()
            .snapshot(&vm, process.pid(), posix_thread.tid())
            .to_string();

        Ok(Box::new(SyscallFileHandle {
            thread: Arc::downgrade(&thread),
            vm,
            snapshot,
        }))
    }
}

struct SyscallFileHandle {
    // Keep the original thread identity across exec's main-thread promotion.
    thread: Weak<Thread>,
    vm: VmarSnapshot,
    snapshot: String,
}

fn check_access(thread: &Thread) -> Result<()> {
    thread
        .as_posix_thread()
        .unwrap()
        .check_alien_access_from(
            current_thread!().as_posix_thread().unwrap(),
            AlienAccessMode::READ_WITH_FS_CREDS,
        )
        .map_err(|_| Error::with_message(Errno::EACCES, "alien access is denied"))
}

impl FileOps for SyscallFileHandle {
    fn read_at(
        &self,
        offset: usize,
        writer: &mut VmWriter,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        let Some(thread) = self.thread.upgrade() else {
            return Ok(0);
        };
        let Some(process) = thread.as_posix_thread().unwrap().weak_process().upgrade() else {
            return Ok(0);
        };
        let vmar_guard = process.lock_vmar();
        if !vmar_guard.is_same_as(&self.vm) {
            return Ok(0);
        }
        // Recheck on every read, including cached chunks and transferred FDs.
        // Holding the VMAR lock through the copy excludes exec credential races.
        check_access(&thread)?;
        let bytes = diagnostics::snapshot_slice(self.snapshot.as_bytes(), offset, writer.avail());
        let mut reader = VmReader::from(bytes);
        Ok(writer.write_fallible(&mut reader).map_err(|(err, _)| err)?)
    }

    fn write_at(
        &self,
        _offset: usize,
        _reader: &mut VmReader,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EPERM, "the syscall snapshot is not writable");
    }
}

impl Pollable for SyscallFileHandle {
    fn poll(&self, mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        IoEvents::IN & mask
    }
}

impl PerOpenFileOps for SyscallFileHandle {
    fn check_seekable(&self) -> Result<()> {
        Ok(())
    }

    fn is_offset_aware(&self) -> bool {
        true
    }
}
