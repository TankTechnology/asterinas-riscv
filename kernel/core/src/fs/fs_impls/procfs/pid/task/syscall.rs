// SPDX-License-Identifier: MPL-2.0

use alloc::format;

use super::TidDirOps;
use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
    thread::Thread,
};

/// Represents the inode at `/proc/[pid]/syscall` (and `/proc/[pid]/task/[tid]/syscall`).
///
/// Format: `<syscall_nr> <arg0> ... <arg5> 0x<sp> 0x<pc>`, or `running` when
/// the thread is not inside a syscall.  Reading never blocks; it is a sample,
/// which is enough for syscall attribution without ptrace.
pub(super) struct SyscallFileOps(TidDirOps);

impl SyscallFileOps {
    pub(super) fn new_inode(dir: &TidDirOps, parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self(dir.clone()), parent, mkmod!(a+r))
    }
}

impl ProcFileOps for SyscallFileOps {
    fn owner_thread(&self) -> Option<Arc<Thread>> {
        self.0.thread()
    }

    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let Some(thread) = self.0.thread() else {
            return_errno_with_message!(Errno::ESRCH, "the thread does not exist");
        };

        let state = thread.syscall_state();
        let text = if state.active {
            format!(
                "{} 0x{:x} 0x{:x} 0x{:x} 0x{:x} 0x{:x} 0x{:x} 0x{:x} 0x{:x}\n",
                state.nr,
                state.args[0],
                state.args[1],
                state.args[2],
                state.args[3],
                state.args[4],
                state.args[5],
                state.sp,
                state.pc
            )
        } else {
            alloc::string::String::from("running\n")
        };

        let bytes = text.as_bytes();
        let mut reader = VmReader::from(&bytes[offset.min(bytes.len())..]);
        let bytes_read = writer.write_fallible(&mut reader)?;
        Ok(bytes_read)
    }
}
