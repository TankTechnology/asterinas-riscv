// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{ipc::IpcFlags, prelude::*};

pub fn sys_shmget(key: i32, size: usize, shmflg: i32, ctx: &Context) -> Result<SyscallReturn> {
    let flags = IpcFlags::from_bits_truncate(shmflg.cast_unsigned());
    let mode: u16 = (shmflg.cast_unsigned() & 0x1FF) as u16;

    debug!(
        "shmget: key = {}, size = {}, flags = {:?}, mode = {:03o}",
        key, size, flags, mode
    );

    let ns_proxy = ctx.thread_local.borrow_ns_proxy();
    let ipc_ns = ns_proxy.unwrap().ipc_ns();

    let credentials = ctx.posix_thread.credentials();
    let pid = ctx.process.pid();
    let shmid = ipc_ns.get_or_create_shm_set(key, size, flags, mode, pid, credentials)?;

    Ok(SyscallReturn::Return(shmid.get() as isize))
}
