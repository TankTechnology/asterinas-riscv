// SPDX-License-Identifier: MPL-2.0

//! The `kcmp(2)` syscall.
//!
//! systemd uses `KCMP_FILE` while setting up service and login-manager
//! resources.  Linux compares kernel objects rather than the numeric file
//! descriptors, so duplicated descriptors must compare equal as well.

use crate::{
    fs::file::{FileLike, file_table::FileDesc},
    prelude::*,
    process::{
        Pid, pid_table,
        posix_thread::{AsPosixThread, alien_access::AlienAccessMode},
    },
    syscall::SyscallReturn,
};

/// Compare two kernel objects belonging to two processes.
pub fn sys_kcmp(
    pid1: Pid,
    pid2: Pid,
    comparison_type: u32,
    idx1: u64,
    idx2: u64,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let process1 = process_for_pid(pid1, ctx)?;
    let process2 = process_for_pid(pid2, ctx)?;
    check_access(&process1, ctx)?;
    check_access(&process2, ctx)?;

    let equal = match comparison_type {
        KCMP_FILE => compare_files(&process1, &process2, idx1, idx2)?,
        KCMP_FILES => compare_file_tables(&process1, &process2)?,
        _ => {
            return_errno_with_message!(Errno::EOPNOTSUPP, "kcmp comparison type is not supported");
        }
    };

    Ok(SyscallReturn::Return(if equal { 0 } else { 1 }))
}

fn check_access(process: &crate::process::Process, ctx: &Context) -> Result<()> {
    let target = process.main_thread();
    ctx.posix_thread.check_alien_access_from(
        target.as_posix_thread().unwrap(),
        AlienAccessMode::READ_WITH_REAL_CREDS,
    )
}

const KCMP_FILE: u32 = 0;
const KCMP_FILES: u32 = 2;

fn process_for_pid(pid: Pid, ctx: &Context) -> Result<Arc<crate::process::Process>> {
    if pid == 0 || pid == ctx.process.pid() {
        return Ok(ctx.process.clone());
    }

    pid_table::pid_table_mut()
        .get_process(pid)
        .ok_or_else(|| Error::with_message(Errno::ESRCH, "the process does not exist"))
}

fn compare_files(
    process1: &crate::process::Process,
    process2: &crate::process::Process,
    idx1: u64,
    idx2: u64,
) -> Result<bool> {
    let fd1 = file_desc(idx1)?;
    let fd2 = file_desc(idx2)?;
    let file1 = file_for_process(process1, fd1)?;
    let file2 = file_for_process(process2, fd2)?;
    Ok(Arc::ptr_eq(&file1, &file2))
}

fn compare_file_tables(
    process1: &crate::process::Process,
    process2: &crate::process::Process,
) -> Result<bool> {
    let thread1 = process1.main_thread();
    let thread2 = process2.main_thread();
    let table1 = thread1.as_posix_thread().unwrap().file_table().lock();
    let table2 = thread2.as_posix_thread().unwrap().file_table().lock();
    let table1 = table1
        .as_ref()
        .ok_or_else(|| Error::with_message(Errno::ESRCH, "the process file table is gone"))?;
    let table2 = table2
        .as_ref()
        .ok_or_else(|| Error::with_message(Errno::ESRCH, "the process file table is gone"))?;

    let table1_guard = table1.read();
    let table2_guard = table2.read();
    Ok(core::ptr::eq(&*table1_guard, &*table2_guard))
}

fn file_for_process(process: &crate::process::Process, fd: FileDesc) -> Result<Arc<dyn FileLike>> {
    let thread = process.main_thread();
    let table = thread.as_posix_thread().unwrap().file_table().lock();
    let table = table
        .as_ref()
        .ok_or_else(|| Error::with_message(Errno::ESRCH, "the process file table is gone"))?;
    let table_guard = table.read();
    Ok(table_guard.get_file(fd)?.clone())
}

fn file_desc(index: u64) -> Result<FileDesc> {
    let index = i32::try_from(index)
        .map_err(|_| Error::with_message(Errno::EBADF, "the file descriptor is invalid"))?;
    index.try_into()
}
