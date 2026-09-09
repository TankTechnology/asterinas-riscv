// SPDX-License-Identifier: MPL-2.0

use align_ext::AlignExt;

use super::PermissionMode;
use crate::{
    ipc::{IpcId, IpcNamespace},
    prelude::*,
    vm::{perms::VmPerms, vmar::VmarMapOffset},
};

bitflags! {
    pub struct ShmFlags: u32 {
        /// Attach read-only.
        const SHM_RDONLY = 0x1000;
        /// Round the attach address down to `SHMLBA` (page) boundary.
        const SHM_RND = 0x2000;
        /// Replace any existing mapping in the attach range.
        const SHM_REMAP = 0x4000;
        /// Grant execution access to the segment.
        const SHM_EXEC = 0x100000;
    }
}

/// Attaches the shared memory segment identified by `shmid` to the calling
/// process and returns the virtual address of the attachment.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/ipc/shm.c#L1654>.
pub fn shm_attach(
    shmid: IpcId,
    shmaddr: Vaddr,
    shmflg: ShmFlags,
    ipc_ns: &Arc<IpcNamespace>,
    ctx: &Context,
) -> Result<Vaddr> {
    let mut required_perm = PermissionMode::READ;
    if !shmflg.contains(ShmFlags::SHM_RDONLY) {
        required_perm |= PermissionMode::WRITE;
    }
    if shmflg.contains(ShmFlags::SHM_EXEC) {
        required_perm |= PermissionMode::EXECUTE;
    }

    let pid = ctx.process.pid();
    let (vmo, size) = ipc_ns.with_shm_set(shmid, required_perm, |shm_set| {
        // Reserve the attachment while the ID-table read lock is held. This
        // prevents a concurrent IPC_RMID from destroying the segment while
        // the address-space mapping is being built.
        shm_set.attach(pid);
        Ok((shm_set.vmo().clone(), shm_set.size()))
    })?;
    let size = size.align_up(PAGE_SIZE);

    let mut vm_perms = VmPerms::READ;
    if !shmflg.contains(ShmFlags::SHM_RDONLY) {
        vm_perms |= VmPerms::WRITE;
    }
    if shmflg.contains(ShmFlags::SHM_EXEC) {
        vm_perms |= VmPerms::EXEC;
    }

    let user_space = ctx.user_space();
    let vmar = user_space.vmar();
    let map_result = (|| {
        let mut options = vmar.new_map(size, vm_perms)?;
        options = options.is_shared(true).vmo(vmo);

        if shmaddr != 0 {
            let addr = if shmflg.contains(ShmFlags::SHM_RND) {
                shmaddr.align_down(PAGE_SIZE)
            } else {
                if !shmaddr.is_multiple_of(PAGE_SIZE) {
                    return_errno_with_message!(Errno::EINVAL, "shmaddr is not aligned");
                }
                shmaddr
            };
            let offset = if shmflg.contains(ShmFlags::SHM_REMAP) {
                VmarMapOffset::FixedReplace(addr)
            } else {
                VmarMapOffset::FixedNoReplace(addr)
            };
            options = options.offset(offset);
        }

        options.build()
    })();
    let map_addr = match map_result {
        Ok(addr) => addr,
        Err(error) => {
            ipc_ns.release_shm_attachment(shmid, pid)?;
            return Err(error);
        }
    };
    ipc_ns.record_shm_attachment(pid, map_addr, shmid);

    Ok(map_addr)
}

/// Detaches the shared memory segment attached at `shmaddr`.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.18/source/ipc/shm.c#L1728>.
pub fn shm_detach(shmaddr: Vaddr, ipc_ns: &Arc<IpcNamespace>, ctx: &Context) -> Result<()> {
    if !shmaddr.is_multiple_of(PAGE_SIZE) {
        return_errno_with_message!(Errno::EINVAL, "shmaddr is not aligned");
    }

    let pid = ctx.process.pid();
    let shmid = ipc_ns.shm_attachment(pid, shmaddr).ok_or_else(|| {
        Error::with_message(Errno::EINVAL, "no shared memory attached at the address")
    })?;

    let user_space = ctx.user_space();
    let vmar = user_space.vmar();
    let range = {
        let guard = vmar.query(shmaddr..shmaddr + PAGE_SIZE);
        let mapping = guard
            .iter()
            .next()
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "the address is not mapped"))?;
        mapping.map_to_addr()..mapping.map_end()
    };
    vmar.remove_mapping(range)?;

    let removed_shmid = ipc_ns.remove_shm_attachment(pid, shmaddr);
    debug_assert_eq!(removed_shmid, Some(shmid));
    ipc_ns.release_shm_attachment(shmid, pid)?;

    Ok(())
}
