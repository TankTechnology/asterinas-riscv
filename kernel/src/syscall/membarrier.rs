// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::prelude::*;

/// `membarrier(2)` — issue memory barriers on a set of threads.
///
/// Asterinas currently implements global and private expedited barriers by
/// synchronizing every online CPU. This is stronger than filtering targets to
/// CPUs running registered or same-address-space threads, but provides the
/// required userspace ordering guarantee. On RISC-V, the sync-core variant
/// additionally performs a system-wide instruction-cache synchronization
/// through SBI RFENCE.
///
/// Reference: <https://docs.kernel.org/scheduler/membarrier.html>.
pub fn sys_membarrier(cmd: u32, flags: u32, _cpu_id: i32, ctx: &Context) -> Result<SyscallReturn> {
    const QUERY: u32 = 0;
    const GLOBAL: u32 = 1 << 0;
    const GLOBAL_EXPEDITED: u32 = 1 << 1;
    const REGISTER_GLOBAL_EXPEDITED: u32 = 1 << 2;
    const PRIVATE_EXPEDITED: u32 = 1 << 3;
    const REGISTER_PRIVATE_EXPEDITED: u32 = 1 << 4;
    const PRIVATE_EXPEDITED_SYNC_CORE: u32 = 1 << 5;
    const REGISTER_PRIVATE_EXPEDITED_SYNC_CORE: u32 = 1 << 6;
    const GET_REGISTRATIONS: u32 = 1 << 9;

    if flags != 0 {
        return_errno_with_message!(Errno::EINVAL, "membarrier flags are unsupported");
    }

    const BASE_SUPPORTED: u32 = GLOBAL
        | GLOBAL_EXPEDITED
        | REGISTER_GLOBAL_EXPEDITED
        | PRIVATE_EXPEDITED
        | REGISTER_PRIVATE_EXPEDITED
        | GET_REGISTRATIONS;
    #[cfg(target_arch = "riscv64")]
    const SUPPORTED: u32 =
        BASE_SUPPORTED | PRIVATE_EXPEDITED_SYNC_CORE | REGISTER_PRIVATE_EXPEDITED_SYNC_CORE;
    #[cfg(not(target_arch = "riscv64"))]
    const SUPPORTED: u32 = BASE_SUPPORTED;

    let user_space = ctx.user_space();
    let process_vm = user_space.vmar().process_vm();

    match cmd {
        QUERY => Ok(SyscallReturn::Return(SUPPORTED as isize)),
        GLOBAL | GLOBAL_EXPEDITED => {
            // Sending the barrier to every CPU also covers every thread whose
            // address space registered for global expedited barriers.
            ostd::smp::synchronize_all_cpus();
            Ok(SyscallReturn::Return(0))
        }
        REGISTER_GLOBAL_EXPEDITED => {
            if process_vm.membarrier_registrations() & REGISTER_GLOBAL_EXPEDITED == 0 {
                ostd::smp::synchronize_all_cpus();
                process_vm.register_membarrier(REGISTER_GLOBAL_EXPEDITED);
            }
            Ok(SyscallReturn::Return(0))
        }
        REGISTER_PRIVATE_EXPEDITED => {
            if process_vm.membarrier_registrations() & REGISTER_PRIVATE_EXPEDITED == 0 {
                ostd::smp::synchronize_all_cpus();
                process_vm.register_membarrier(REGISTER_PRIVATE_EXPEDITED);
            }
            Ok(SyscallReturn::Return(0))
        }
        PRIVATE_EXPEDITED => {
            if process_vm.membarrier_registrations() & REGISTER_PRIVATE_EXPEDITED == 0 {
                return_errno_with_message!(
                    Errno::EPERM,
                    "private expedited membarrier is not registered"
                );
            }
            ostd::smp::synchronize_all_cpus();
            Ok(SyscallReturn::Return(0))
        }
        REGISTER_PRIVATE_EXPEDITED_SYNC_CORE => {
            #[cfg(not(target_arch = "riscv64"))]
            return_errno_with_message!(Errno::EINVAL, "sync-core membarrier is unsupported");

            #[cfg(target_arch = "riscv64")]
            {
                if process_vm.membarrier_registrations() & REGISTER_PRIVATE_EXPEDITED_SYNC_CORE == 0
                {
                    ostd::smp::synchronize_all_cpus();
                    process_vm.register_membarrier(REGISTER_PRIVATE_EXPEDITED_SYNC_CORE);
                }
                Ok(SyscallReturn::Return(0))
            }
        }
        PRIVATE_EXPEDITED_SYNC_CORE => {
            #[cfg(not(target_arch = "riscv64"))]
            return_errno_with_message!(Errno::EINVAL, "sync-core membarrier is unsupported");

            #[cfg(target_arch = "riscv64")]
            {
                if process_vm.membarrier_registrations() & REGISTER_PRIVATE_EXPEDITED_SYNC_CORE == 0
                {
                    return_errno_with_message!(
                        Errno::EPERM,
                        "private expedited sync-core membarrier is not registered"
                    );
                }
                ostd::smp::synchronize_all_cpus();
                ostd::arch::flush_icache(false)?;
                Ok(SyscallReturn::Return(0))
            }
        }
        GET_REGISTRATIONS => Ok(SyscallReturn::Return(
            process_vm.membarrier_registrations() as isize
        )),
        _ => return_errno_with_message!(Errno::EINVAL, "membarrier command is unsupported"),
    }
}
