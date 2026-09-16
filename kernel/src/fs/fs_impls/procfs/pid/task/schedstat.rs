// SPDX-License-Identifier: MPL-2.0

//! Per-thread scheduling statistics exposed through `/proc/[pid]/schedstat` and
//! `/proc/[pid]/task/[tid]/schedstat`.
//!
//! Linux documents the three fields as CPU runtime, runnable-queue delay, and
//! the number of timeslices run. The first two fields are nanoseconds.

use alloc::format;

use super::TidDirOps;
use crate::{
    fs::{
        file::{AccessMode, PerOpenFileOps, StatusFlags, mkmod},
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
    process::posix_thread::AsPosixThread,
    thread::Thread,
};

/// Represents the inode at `/proc/[pid]/task/[tid]/schedstat` (and also
/// `/proc/[pid]/schedstat`).
pub struct SchedstatFileOps(TidDirOps);

impl SchedstatFileOps {
    pub fn new_inode(dir: &TidDirOps, parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self(dir.clone()), parent, mkmod!(a+r))
    }
}

impl ProcFileOps for SchedstatFileOps {
    fn owner_thread(&self) -> Option<Arc<Thread>> {
        self.0.thread()
    }

    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let Some((thread, _)) = self.0.thread_and_process() else {
            return_errno_with_message!(Errno::ESRCH, "the thread or the process does not exist");
        };

        let frequency = ostd::arch::tsc_freq();
        let Some(snapshot) = thread
            .sched_attr()
            .sched_info()
            .snapshot_at_frequency(frequency)
        else {
            return_errno_with_message!(Errno::EIO, "the scheduler clock frequency is zero");
        };

        let runtime_ns = u64::try_from(
            thread
                .as_posix_thread()
                .unwrap()
                .prof_clock()
                .read_time()
                .as_nanos(),
        )
        .unwrap_or(u64::MAX);
        let output = format_schedstat(runtime_ns, snapshot.run_delay_ns, snapshot.dispatches);
        let mut reader = VmReader::from(&output.as_bytes()[offset.min(output.len())..]);

        Ok(writer.write_fallible(&mut reader).map_err(|(err, _)| err)?)
    }

    fn open(
        &self,
        access_mode: AccessMode,
        _status_flags: StatusFlags,
    ) -> Option<Result<Box<dyn PerOpenFileOps>>> {
        access_mode.is_writable().then(|| {
            Err(Error::with_message(
                Errno::EACCES,
                "schedstat is not writable",
            ))
        })
    }
}

fn format_schedstat(runtime_ns: u64, run_delay_ns: u64, dispatches: u64) -> String {
    format!("{runtime_ns} {run_delay_ns} {dispatches}\n")
}

#[cfg(ktest)]
mod tests {
    use alloc::format;

    use ostd::prelude::ktest;

    use super::format_schedstat;
    use crate::fs::procfs::pid::task::TidDirOps;

    #[ktest]
    fn schedstat() {
        assert_eq!(format_schedstat(17, 23, 5), "17 23 5\n");
        assert_eq!(
            format_schedstat(u64::MAX, u64::MAX, u64::MAX),
            format!("{} {} {}\n", u64::MAX, u64::MAX, u64::MAX)
        );

        let entry_count = TidDirOps::STATIC_ENTRIES
            .iter()
            .filter(|(name, _, _)| *name == "schedstat")
            .count();
        assert_eq!(entry_count, 1);
    }
}
