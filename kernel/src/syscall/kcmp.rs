// SPDX-License-Identifier: MPL-2.0

//! The `kcmp(2)` system call.

use ostd::sync::RoArc;

use crate::{
    fs::file::{
        FileLike,
        file_table::{FileDesc, FileTable},
    },
    prelude::*,
    process::{
        Pid, Process, pid_table,
        posix_thread::{AsPosixThread, alien_access::AlienAccessMode},
    },
    syscall::SyscallReturn,
};

const KCMP_FILE: u32 = 0;
const KCMP_FILES: u32 = 2;
const KCMP_TYPES: u32 = 8;
const KCMP_UNEQUAL_NO_ORDER: isize = 3;

/// Compares whether two processes refer to the same kernel resource.
///
/// This initial implementation supports the file-description and file-table
/// comparisons needed by service managers. Other Linux-defined comparison
/// types fail explicitly instead of silently producing a false result.
pub fn sys_kcmp(
    pid1: Pid,
    pid2: Pid,
    comparison_type: u32,
    idx1: u64,
    idx2: u64,
    ctx: &Context,
) -> Result<SyscallReturn> {
    debug!(
        "pid1 = {}, pid2 = {}, type = {}, idx1 = {}, idx2 = {}",
        pid1, pid2, comparison_type, idx1, idx2
    );

    let process1 = process_for_pid(pid1)?;
    let process2 = process_for_pid(pid2)?;
    check_access(&process1, ctx)?;
    check_access(&process2, ctx)?;

    let equal = match comparison_type {
        KCMP_FILE => compare_files(&process1, &process2, idx1, idx2)?,
        KCMP_FILES => compare_file_tables(&process1, &process2)?,
        KCMP_TYPES.. => {
            return_errno_with_message!(Errno::EINVAL, "invalid kcmp comparison type");
        }
        _ => {
            return_errno_with_message!(
                Errno::EOPNOTSUPP,
                "the kcmp comparison type is not supported"
            );
        }
    };

    let comparison = if equal { 0 } else { KCMP_UNEQUAL_NO_ORDER };
    Ok(SyscallReturn::Return(comparison))
}

fn process_for_pid(pid: Pid) -> Result<Arc<Process>> {
    pid_table::pid_table_mut()
        .get_process(pid)
        .ok_or_else(|| Error::with_message(Errno::ESRCH, "the process does not exist"))
}

fn check_access(process: &Process, ctx: &Context) -> Result<()> {
    process
        .main_thread()
        .as_posix_thread()
        .unwrap()
        .check_alien_access_from(ctx.posix_thread, AlienAccessMode::READ_WITH_REAL_CREDS)
}

fn compare_files(process1: &Process, process2: &Process, idx1: u64, idx2: u64) -> Result<bool> {
    let file1 = file_for_process(process1, file_desc(idx1)?)?;
    let file2 = file_for_process(process2, file_desc(idx2)?)?;
    Ok(Arc::ptr_eq(&file1, &file2))
}

fn compare_file_tables(process1: &Process, process2: &Process) -> Result<bool> {
    let table1 = file_table_for_process(process1)?;
    let table2 = file_table_for_process(process2)?;
    Ok(table1.ptr_eq(&table2))
}

fn file_for_process(process: &Process, fd: FileDesc) -> Result<Arc<dyn FileLike>> {
    let table = file_table_for_process(process)?;
    let file = table.read().get_file(fd)?.clone();
    Ok(file)
}

fn file_table_for_process(process: &Process) -> Result<RoArc<FileTable>> {
    let main_thread = process.main_thread();
    let posix_thread = main_thread.as_posix_thread().unwrap();
    posix_thread
        .file_table()
        .lock()
        .as_ref()
        .cloned()
        .ok_or_else(|| Error::with_message(Errno::ESRCH, "the process file table is gone"))
}

fn file_desc(index: u64) -> Result<FileDesc> {
    let raw_fd = i32::try_from(index)
        .map_err(|_| Error::with_message(Errno::EBADF, "the file descriptor is invalid"))?;
    raw_fd.try_into()
}
