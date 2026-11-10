// SPDX-License-Identifier: MPL-2.0

//! Per-process I/O statistics exposed through `/proc/[pid]/io` and
//! `/proc/[pid]/task/[tid]/io`.
//!
//! The layout follows Linux's `proc_pid_io_accounting`
//! (<https://man7.org/linux/man-pages/man5/proc_pid_io.5.html>):
//!
//! - `rchar`: bytes returned by read-family syscalls.
//! - `wchar`: bytes accepted by write-family syscalls.
//! - `syscr`: number of read-family syscalls.
//! - `syscw`: number of write-family syscalls.
//! - `read_bytes`: bytes fetched from storage.
//! - `write_bytes`: bytes sent to storage.
//! - `cancelled_write_bytes`: bytes written and then discarded. No subsystem
//!   reports this yet, so it is always zero.
//!
//! The counters are kept per process, so the per-thread file reports the same
//! values as the process file. See [`IoAccounting`] for how each counter is
//! updated.
//!
//! [`IoAccounting`]: crate::process::IoAccounting

use alloc::format;

use super::TidDirOps;
use crate::{
    fs::{
        file::{AccessMode, PerOpenFileOps, StatusFlags, mkmod},
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
    process::IoStats,
    thread::Thread,
};

/// Represents the inode at `/proc/[pid]/task/[tid]/io` (and also `/proc/[pid]/io`).
pub struct IoFileOps(TidDirOps);

impl IoFileOps {
    pub fn new_inode(dir: &TidDirOps, parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference: <https://elixir.bootlin.com/linux/v6.16.5/source/fs/proc/base.c#L3375>
        ProcFile::new(Self(dir.clone()), parent, mkmod!(a+r))
    }
}

impl ProcFileOps for IoFileOps {
    fn owner_thread(&self) -> Option<Arc<Thread>> {
        self.0.thread()
    }

    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let Some(process) = self.0.process() else {
            return_errno_with_message!(Errno::ESRCH, "the process does not exist");
        };

        let output = format_io(process.io_accounting().snapshot());
        let mut reader = VmReader::from(&output.as_bytes()[offset.min(output.len())..]);

        Ok(writer.write_fallible(&mut reader).map_err(|(err, _)| err)?)
    }

    fn open(
        &self,
        access_mode: AccessMode,
        _status_flags: StatusFlags,
    ) -> Option<Result<Box<dyn PerOpenFileOps>>> {
        access_mode
            .is_writable()
            .then(|| Err(Error::with_message(Errno::EACCES, "io is not writable")))
    }
}

fn format_io(stats: IoStats) -> String {
    format!(
        "rchar: {}\n\
         wchar: {}\n\
         syscr: {}\n\
         syscw: {}\n\
         read_bytes: {}\n\
         write_bytes: {}\n\
         cancelled_write_bytes: {}\n",
        stats.rchar,
        stats.wchar,
        stats.syscr,
        stats.syscw,
        stats.read_bytes,
        stats.write_bytes,
        stats.cancelled_write_bytes,
    )
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn io_format_matches_proc() {
        let stats = IoStats {
            rchar: 1,
            wchar: 2,
            syscr: 3,
            syscw: 4,
            read_bytes: 5,
            write_bytes: 6,
            cancelled_write_bytes: 7,
            min_flt: 8,
            maj_flt: 9,
        };
        assert_eq!(
            format_io(stats),
            "rchar: 1\nwchar: 2\nsyscr: 3\nsyscw: 4\nread_bytes: 5\nwrite_bytes: 6\ncancelled_write_bytes: 7\n"
        );
    }

    #[ktest]
    fn io_is_listed_once() {
        let entry_count = TidDirOps::STATIC_ENTRIES
            .iter()
            .filter(|(name, _, _)| *name == "io")
            .count();
        assert_eq!(entry_count, 1);
    }
}
