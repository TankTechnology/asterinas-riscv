// SPDX-License-Identifier: MPL-2.0

use ostd::mm::VmIo;

use super::SyscallReturn;
use crate::{
    ipc::{IpcControlCmd, IpcId, shared_memory::PermissionMode},
    prelude::*,
    process::credentials::capabilities::CapSet,
    security::lsm::hooks as lsm_hooks,
};

pub fn sys_shmctl(shmid: i32, op: i32, arg: Vaddr, ctx: &Context) -> Result<SyscallReturn> {
    let Ok(shmid) = IpcId::try_from(shmid.cast_unsigned()) else {
        return_errno_with_message!(Errno::EINVAL, "non-positive shared memory IDs are invalid");
    };
    let cmd = IpcControlCmd::try_from(op)?;

    debug!(
        "shmctl: shmid = {:?}, cmd = {:?}, arg = {:x}",
        shmid, cmd, arg
    );

    let ns_proxy = ctx.thread_local.borrow_ns_proxy();
    let ipc_ns = ns_proxy.unwrap().ipc_ns();

    match cmd {
        IpcControlCmd::IPC_RMID => {
            let euid = ctx.posix_thread.credentials().euid();
            let has_sys_admin = lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
                ipc_ns.owner().as_ref(),
                ctx.posix_thread,
                CapSet::SYS_ADMIN,
            ))
            .is_ok();
            ipc_ns.mark_shm_set_for_removal(shmid, |shm_set| {
                let permission = shm_set.permission();
                let can_remove =
                    (euid == permission.uid()) || (euid == permission.cuid()) || has_sys_admin;
                if !can_remove {
                    return_errno_with_message!(
                        Errno::EPERM,
                        "the process does not have permission to remove the segment"
                    );
                }

                Ok(())
            })?;
        }
        IpcControlCmd::IPC_STAT => {
            ipc_ns.with_shm_set(shmid, PermissionMode::READ, |shm_set| {
                let shmid_ds = shm_set.shmid_ds();
                Ok(ctx.user_space().write_val(arg, &shmid_ds)?)
            })?;
        }
        _ => {
            return_errno_with_message!(Errno::EINVAL, "unsupported command");
        }
    }

    Ok(SyscallReturn::Return(0))
}
