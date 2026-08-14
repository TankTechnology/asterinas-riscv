// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{ipc::shared_memory::shm::shm_detach, prelude::*};

pub fn sys_shmdt(shmaddr: Vaddr, ctx: &Context) -> Result<SyscallReturn> {
    debug!("shmdt: shmaddr = {:#x}", shmaddr);

    let ns_proxy = ctx.thread_local.borrow_ns_proxy();
    let ipc_ns = ns_proxy.unwrap().ipc_ns();

    shm_detach(shmaddr, ipc_ns, ctx)?;

    Ok(SyscallReturn::Return(0))
}
