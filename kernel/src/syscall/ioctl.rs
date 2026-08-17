// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    fs::file::{
        FileLike,
        file_table::{FdFlags, RawFileDesc, WithFileTable, get_file_fast},
    },
    prelude::*,
    process::posix_thread::FileTableRefMut,
    util::ioctl::{RawIoctl, dispatch_ioctl},
};

pub fn sys_ioctl(
    raw_fd: RawFileDesc,
    cmd: u32,
    arg: Vaddr,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let raw_ioctl = RawIoctl::new(cmd, arg);
    debug!("raw_fd = {}, raw_ioctl = {:#x?}", raw_fd, raw_ioctl,);

    let mut file_table = ctx.thread_local.borrow_file_table_mut();

    // First, handle the ioctl command that affects the file descriptor.
    if let Some(res) = handle_fd_ioctl(&mut file_table, raw_fd, raw_ioctl) {
        res?;
        return Ok(SyscallReturn::Return(0));
    }

    let file = get_file_fast!(&mut file_table, raw_fd.try_into()?);

    // Handle file-level ioctls (FIONBIO, FIOASYNC) on the borrowed file.
    if let Some(res) = handle_file_ioctl(&**file, raw_ioctl) {
        res?;
        return Ok(SyscallReturn::Return(0));
    }

    // Clone to release the borrow on file_table before calling ioctl_with_table
    // (which needs to borrow file_table again).
    let file_owned = file.into_owned();

    // Try ioctl_with_table first (e.g., LOOP_SET_FD needs file_table access).
    if let Some(res) = file_owned.ioctl_with_table(raw_ioctl, &mut file_table) {
        return Ok(SyscallReturn::Return(res? as isize));
    }

    // We have to drop `file_table` because some I/O command will modify the
    // file table (e.g., TIOCGPTPEER).
    drop(file_table);
    let res = file_owned.ioctl(raw_ioctl)?;
    Ok(SyscallReturn::Return(res as isize))
}

mod ioctl_defs {
    use crate::util::ioctl::{InData, NoData, ioc};

    pub(super) type SetNonBlocking    = ioc!(FIONBIO,  0x5421, InData<i32>);
    pub(super) type SetAsync          = ioc!(FIOASYNC, 0x5452, InData<i32>);

    pub(super) type SetNotCloseOnExec = ioc!(FIONCLEX, 0x5450, NoData);
    pub(super) type SetCloseOnExec    = ioc!(FIOCLEX,  0x5451, NoData);
}

fn handle_fd_ioctl(
    file_table: &mut FileTableRefMut,
    raw_fd: RawFileDesc,
    raw_ioctl: RawIoctl,
) -> Option<Result<()>> {
    use ioctl_defs::*;

    dispatch_ioctl!(match raw_ioctl {
        SetNotCloseOnExec => {
            Some(file_table.read_with(|inner| {
                let entry = inner.get_entry(raw_fd.try_into()?)?;
                entry.set_flags(entry.flags() - FdFlags::CLOEXEC);
                Ok(())
            }))
        }
        SetCloseOnExec => {
            Some(file_table.read_with(|inner| {
                let entry = inner.get_entry(raw_fd.try_into()?)?;
                entry.set_flags(entry.flags() | FdFlags::CLOEXEC);
                Ok(())
            }))
        }
        _ => None,
    })
}

fn handle_file_ioctl(file: &dyn FileLike, raw_ioctl: RawIoctl) -> Option<Result<()>> {
    use ioctl_defs::*;

    dispatch_ioctl!(match raw_ioctl {
        cmd @ SetNonBlocking => {
            let handler = || {
                let is_nonblocking = cmd.read()? != 0;

                file.update_status_nonblock(is_nonblocking);
                Ok(())
            };
            Some(handler())
        }
        cmd @ SetAsync => {
            let handler = || {
                let is_async = cmd.read()? != 0;

                file.update_status_async(is_async)
            };
            Some(handler())
        }
        _ => None,
    })
}