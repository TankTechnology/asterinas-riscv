// SPDX-License-Identifier: MPL-2.0

use ostd::{const_assert, mm::VmIo};

use super::{
    SyscallReturn,
    sched_get_priority_max::{rt_to_static, sched_priority_range, static_to_rt},
};
use crate::{
    prelude::*,
    process::{
        ResourceType::{RLIMIT_NICE, RLIMIT_RTPRIO},
        credentials::capabilities::CapSet,
        pid_table,
        posix_thread::{AsPosixThread, PosixThread},
    },
    sched::{LinuxSchedPolicy, Nice, RealTimePolicy, SchedAttr, SchedPolicy},
    security::lsm::hooks as lsm_hooks,
    thread::{AsThread, Thread, Tid},
    util::CopyCompat,
};

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(super) struct LinuxSchedAttr {
    // Size of this structure
    pub(super) size: u32,

    // Policy (SCHED_*)
    pub(super) sched_policy: u32,
    // Flags
    pub(super) sched_flags: u64,

    // Nice value (SCHED_NORMAL, SCHED_BATCH)
    pub(super) sched_nice: i32,

    // Static priority (SCHED_FIFO, SCHED_RR)
    pub(super) sched_priority: u32,

    // For SCHED_DEADLINE
    pub(super) sched_runtime: u64,
    pub(super) sched_deadline: u64,
    pub(super) sched_period: u64,

    // Utilization hints
    pub(super) sched_util_min: u32,
    pub(super) sched_util_max: u32,
}

// Reference: <https://elixir.bootlin.com/linux/v6.17.7/source/include/uapi/linux/sched/types.h#L7>
const SCHED_ATTR_SIZE_VER0: u32 = 48;
// Reference: <https://elixir.bootlin.com/linux/v6.17.7/source/include/uapi/linux/sched/types.h#L8>
const SCHED_ATTR_SIZE_VER1: u32 = 56;
// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/linux/sched/types.h#L104>
pub(super) const SCHED_FLAG_RESET_ON_FORK: u64 = 0x01;
// Reference: <https://elixir.bootlin.com/linux/v6.18/source/include/uapi/linux/sched.h#L122>
pub(super) const SCHED_RESET_ON_FORK: u32 = 0x4000_0000;

const_assert!(size_of::<LinuxSchedAttr>() == SCHED_ATTR_SIZE_VER1 as usize);

impl TryFrom<SchedPolicy> for LinuxSchedAttr {
    type Error = Error;

    fn try_from(value: SchedPolicy) -> Result<Self> {
        Ok(match value {
            SchedPolicy::Stop => LinuxSchedAttr {
                sched_policy: LinuxSchedPolicy::Fifo as u32,
                sched_priority: 99, // Linux uses 99 as the default priority for STOP tasks.
                ..Default::default()
            },

            SchedPolicy::RealTime { rt_prio, rt_policy } => LinuxSchedAttr {
                sched_policy: match rt_policy {
                    RealTimePolicy::Fifo => LinuxSchedPolicy::Fifo,
                    RealTimePolicy::RoundRobin { .. } => LinuxSchedPolicy::RoundRobin,
                } as u32,
                sched_priority: rt_to_static(rt_prio),
                ..Default::default()
            },

            // The SCHED_IDLE policy is mapped to the highest nice value of
            // `SchedPolicy::Fair` instead of `SchedPolicy::Idle`. Tasks of the
            // latter policy are invisible to the user API.
            SchedPolicy::Fair(Nice::MAX) => LinuxSchedAttr {
                sched_policy: LinuxSchedPolicy::Idle as u32,
                ..Default::default()
            },

            SchedPolicy::Fair(nice) => LinuxSchedAttr {
                sched_policy: LinuxSchedPolicy::Normal as u32,
                sched_nice: nice.value().get().into(),
                ..Default::default()
            },

            SchedPolicy::Batch(nice) => LinuxSchedAttr {
                sched_policy: LinuxSchedPolicy::Batch as u32,
                sched_nice: nice.value().get().into(),
                ..Default::default()
            },

            SchedPolicy::Idle => return_errno_with_message!(
                Errno::EACCES,
                "scheduling attributes for idle tasks are not accessible"
            ),
        })
    }
}

impl TryFrom<LinuxSchedAttr> for SchedPolicy {
    type Error = Error;

    fn try_from(value: LinuxSchedAttr) -> Result<Self> {
        let linux_policy = LinuxSchedPolicy::try_from(value.sched_policy)?;

        Ok(match linux_policy {
            LinuxSchedPolicy::Fifo | LinuxSchedPolicy::RoundRobin => SchedPolicy::RealTime {
                rt_prio: static_to_rt(value.sched_priority)?,
                rt_policy: match linux_policy {
                    LinuxSchedPolicy::Fifo => RealTimePolicy::Fifo,
                    LinuxSchedPolicy::RoundRobin => RealTimePolicy::RoundRobin {
                        base_slice_factor: None,
                    },
                    _ => unreachable!(),
                },
            },

            _ if value.sched_priority != 0 => {
                return_errno_with_message!(Errno::EINVAL, "invalid scheduling priority")
            }

            LinuxSchedPolicy::Normal => SchedPolicy::Fair(linux_nice(value.sched_nice)?),

            LinuxSchedPolicy::Batch => SchedPolicy::Batch(linux_nice(value.sched_nice)?),

            // The SCHED_IDLE policy is mapped to the highest nice value of
            // `SchedPolicy::Fair` instead of `SchedPolicy::Idle`. Tasks of the
            // latter policy are invisible to the user API.
            LinuxSchedPolicy::Idle => SchedPolicy::Fair(Nice::MAX),

            LinuxSchedPolicy::Iso | LinuxSchedPolicy::Deadline | LinuxSchedPolicy::Ext => {
                return_errno_with_message!(Errno::EINVAL, "invalid scheduling policy")
            }
        })
    }
}

fn linux_nice(value: i32) -> Result<Nice> {
    Ok(Nice::new(
        i8::try_from(value)
            .ok()
            .and_then(|value| value.try_into().ok())
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "invalid nice number"))?,
    ))
}

pub(super) fn read_linux_sched_attr_from_user(
    addr: Vaddr,
    ctx: &Context,
) -> Result<LinuxSchedAttr> {
    // The code below is written according to the Linux implementation.
    // Reference: <https://elixir.bootlin.com/linux/v6.17.7/source/kernel/sched/syscalls.c#L889>

    let user_space = ctx.user_space();

    let raw_size = user_space.read_val::<u32>(addr)?;
    let user_size = if raw_size == 0 {
        SCHED_ATTR_SIZE_VER0
    } else {
        raw_size
    };
    if user_size < SCHED_ATTR_SIZE_VER0 || user_size > PAGE_SIZE as u32 {
        let _ = user_space.write_val(addr, &(size_of::<LinuxSchedAttr>() as u32));
        return_errno_with_message!(Errno::E2BIG, "invalid scheduling attribute size");
    }

    let mut attr = user_space
        .read_val_compat::<LinuxSchedAttr>(addr, user_size as usize)
        .inspect_err(|err| {
            if err.error() == Errno::E2BIG {
                let _ = user_space.write_val(addr, &(size_of::<LinuxSchedAttr>() as u32));
            }
        })?;
    // If `attr.size` is modified concurrently, we should use the original size.
    attr.size = user_size;

    // TODO: Check whether `sched_flags` is valid.

    Ok(attr)
}

pub(super) fn write_linux_sched_attr_to_user(
    mut attr: LinuxSchedAttr,
    addr: Vaddr,
    user_size: u32,
    ctx: &Context,
) -> Result<()> {
    if user_size < SCHED_ATTR_SIZE_VER0 || user_size > PAGE_SIZE as u32 {
        return_errno_with_message!(Errno::EINVAL, "invalid scheduling attribute size");
    }

    attr.size = (size_of::<LinuxSchedAttr>() as u32).min(user_size);

    let linux_policy = LinuxSchedPolicy::try_from(attr.sched_policy)
        .expect("all user-visible scheduling policies should be valid");
    let range = sched_priority_range(linux_policy);
    attr.sched_util_min = *range.start();
    attr.sched_util_max = *range.end();

    ctx.user_space()
        .write_val_compat(addr, user_size as usize, &attr)?
        .ignore_trailing();

    Ok(())
}

pub(super) fn access_sched_attr_with<T>(
    tid: Tid,
    ctx: &Context,
    f: impl FnOnce(&SchedAttr) -> Result<T>,
) -> Result<T> {
    let thread = find_sched_target(tid, ctx)?;
    f(thread.sched_attr())
}

pub(super) fn update_sched_attr_with(
    tid: Tid,
    ctx: &Context,
    reset_on_fork: Option<bool>,
    update_policy: impl FnOnce(SchedPolicy) -> Result<SchedPolicy>,
) -> Result<()> {
    let thread = find_sched_target(tid, ctx)?;
    let attr = thread.sched_attr();
    let old_policy = attr.policy();
    let new_policy = update_policy(old_policy)?;

    check_sched_update_permission(
        thread
            .as_posix_thread()
            .expect("a scheduling syscall target must be a POSIX thread"),
        attr,
        old_policy,
        new_policy,
        reset_on_fork,
        ctx,
    )?;

    attr.set_policy(new_policy);
    if let Some(reset_on_fork) = reset_on_fork {
        attr.set_reset_on_fork(reset_on_fork);
    }
    Ok(())
}

fn find_sched_target(tid: Tid, ctx: &Context) -> Result<Arc<Thread>> {
    if tid.cast_signed() < 0 {
        return_errno_with_message!(Errno::EINVAL, "all negative TIDs are not valid");
    }

    if tid == 0 {
        return Ok(ctx
            .task
            .as_thread()
            .expect("a scheduling syscall caller must be a thread")
            .clone());
    }

    let Some(thread) = pid_table::pid_table_mut().get_thread(tid) else {
        return_errno_with_message!(Errno::ESRCH, "the target thread does not exist");
    };
    Ok(thread)
}

// Reference: <https://github.com/torvalds/linux/blob/v6.18/kernel/sched/syscalls.c#L405-L457>.
fn check_sched_update_permission(
    target: &PosixThread,
    attr: &SchedAttr,
    old_policy: SchedPolicy,
    new_policy: SchedPolicy,
    reset_on_fork: Option<bool>,
    ctx: &Context,
) -> Result<()> {
    let caller_euid = ctx.posix_thread.credentials().euid();
    let target_credentials = target.credentials();
    let same_owner =
        caller_euid == target_credentials.euid() || caller_euid == target_credentials.ruid();
    drop(target_credentials);

    let target_process = target.process();
    let limits = target_process.resource_limits();
    let requires_privilege = !same_owner
        || policy_update_requires_privilege(old_policy, new_policy, limits)
        || (attr.reset_on_fork() && reset_on_fork == Some(false));
    if !requires_privilege {
        return Ok(());
    }

    // Drop the process's namespace lock before entering LSM: the capability
    // module reads the caller's namespace and the caller may be the target.
    let target_user_ns = target_process.user_ns().lock().clone();
    if lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
        target_user_ns.as_ref(),
        ctx.posix_thread,
        CapSet::SYS_NICE,
    ))
    .is_ok()
    {
        return Ok(());
    }

    return_errno_with_message!(
        Errno::EPERM,
        "the caller cannot change the target thread's scheduling attributes"
    )
}

fn policy_update_requires_privilege(
    old_policy: SchedPolicy,
    new_policy: SchedPolicy,
    limits: &crate::process::rlimit::ResourceLimits,
) -> bool {
    match new_policy {
        SchedPolicy::RealTime {
            rt_prio: new_priority,
            rt_policy: new_rt_policy,
        } => {
            let rlimit = limits.get_rlimit(RLIMIT_RTPRIO).get_cur();
            let (same_policy, old_priority) = match old_policy {
                SchedPolicy::RealTime { rt_prio, rt_policy } => {
                    (rt_policy == new_rt_policy, u64::from(rt_to_static(rt_prio)))
                }
                _ => (false, 0),
            };
            let new_priority = u64::from(rt_to_static(new_priority));
            (!same_policy && rlimit == 0) || (new_priority > old_priority && new_priority > rlimit)
        }
        SchedPolicy::Fair(new_nice) | SchedPolicy::Batch(new_nice) => {
            let old_nice = match old_policy {
                SchedPolicy::Fair(nice) | SchedPolicy::Batch(nice) => nice,
                _ => Nice::default(),
            };
            let nice_rlimit = u64::try_from(20 - i32::from(new_nice.value().get()))
                .expect("a valid nice value always maps to a positive rlimit");
            new_nice < old_nice && nice_rlimit > limits.get_rlimit(RLIMIT_NICE).get_cur()
        }
        SchedPolicy::Stop | SchedPolicy::Idle => true,
    }
}

pub fn sys_sched_getattr(
    tid: Tid,
    addr: Vaddr,
    user_size: u32,
    flags: u32,
    ctx: &Context,
) -> Result<SyscallReturn> {
    if addr == 0 {
        return_errno_with_message!(Errno::EINVAL, "invalid user space address");
    }
    if flags != 0 {
        // Linux also has no support for any flags yet.
        return_errno_with_message!(Errno::EINVAL, "invalid flags");
    }

    let (policy, reset_on_fork) =
        access_sched_attr_with(tid, ctx, |attr| Ok((attr.policy(), attr.reset_on_fork())))?;
    let mut attr: LinuxSchedAttr = policy
        .try_into()
        .expect("all user-visible scheduling attributes should be valid");
    if reset_on_fork {
        attr.sched_flags |= SCHED_FLAG_RESET_ON_FORK;
    }
    write_linux_sched_attr_to_user(attr, addr, user_size, ctx)?;

    Ok(SyscallReturn::Return(0))
}
