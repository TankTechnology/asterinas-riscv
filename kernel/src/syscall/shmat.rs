// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{
    ipc::{
        IpcId,
        shared_memory::shm::{ShmFlags, shm_attach},
    },
    prelude::*,
};

pub fn sys_shmat(shmid: i32, shmaddr: Vaddr, shmflg: i32, ctx: &Context) -> Result<SyscallReturn> {
    let Ok(shmid) = IpcId::try_from(shmid.cast_unsigned()) else {
        return_errno_with_message!(Errno::EINVAL, "non-positive shared memory IDs are invalid");
    };
    let flags = ShmFlags::from_bits_truncate(shmflg.cast_unsigned());

    debug!(
        "shmat: shmid = {:?}, shmaddr = {:#x}, shmflg = {:?}",
        shmid, shmaddr, flags
    );

    let ns_proxy = ctx.thread_local.borrow_ns_proxy();
    let ipc_ns = ns_proxy.unwrap().ipc_ns();

    let addr = shm_attach(shmid, shmaddr, flags, ipc_ns, ctx)?;

    Ok(SyscallReturn::Return(addr as isize))
}
