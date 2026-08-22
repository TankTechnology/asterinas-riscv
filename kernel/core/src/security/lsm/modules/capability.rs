// SPDX-License-Identifier: MPL-2.0

use super::super::{
    LsmFlags, LsmModule,
    hooks::{AlienAccessContext, CapableContext, LsmAlienAccessHook, LsmCapabilityHook},
};
use crate::{
    prelude::*,
    process::{credentials::capabilities::CapSet, posix_thread::alien_access::CredsSource},
};

pub(super) static CAPABILITY_LSM: CapabilityLsm = CapabilityLsm;

/// Capability-based authorization checks for built-in kernel operations.
pub(super) struct CapabilityLsm;

impl LsmModule for CapabilityLsm {
    fn name(&self) -> &'static str {
        "capability"
    }

    fn flags(&self) -> LsmFlags {
        LsmFlags::empty()
    }
}

impl LsmCapabilityHook for CapabilityLsm {
    fn on_capable(&self, context: &CapableContext) -> Result<()> {
        // Namespace-aware capability check, following Linux's
        // `ns_capable_common`: the thread passes if, for the target user
        // namespace or one of its ancestors,
        //  1. the thread belongs to that namespace and holds the capability
        //     in its effective set, or
        //  2. the thread belongs to the *parent* of that namespace and is
        //     the namespace's owner (the creator has all capabilities over
        //     the namespaces it created).
        //
        // Note that capabilities held in a child user namespace never apply
        // to resources owned by an ancestor namespace, since the walk only
        // goes upwards from the target.
        //
        // Reference: <https://elixir.bootlin.com/linux/v6.18/source/kernel/capability.c#L384>.
        let credentials = context.posix_thread().credentials();
        let thread_user_ns = context.posix_thread().process().user_ns().lock().clone();

        let mut target_ns = context.target_user_ns();
        loop {
            if core::ptr::eq(thread_user_ns.as_ref(), target_ns) {
                if credentials
                    .effective_capset()
                    .contains(context.required_cap())
                {
                    return Ok(());
                }
                break;
            }

            let Some(parent_ns) = target_ns.parent_ns() else {
                break;
            };
            if core::ptr::eq(thread_user_ns.as_ref(), parent_ns.as_ref())
                && target_ns
                    .owner_uid()
                    .is_ok_and(|owner| owner == credentials.euid())
            {
                return Ok(());
            }

            target_ns = parent_ns;
        }

        return_errno_with_message!(
            Errno::EPERM,
            "the thread does not have the required capability"
        );
    }
}

impl LsmAlienAccessHook for CapabilityLsm {
    fn on_alien_access(&self, context: &AlienAccessContext) -> Result<()> {
        let accessor_cred = context.accessor().credentials();
        let (caller_uid, caller_gid) = match context.mode().creds() {
            CredsSource::FsCreds => (accessor_cred.fsuid(), accessor_cred.fsgid()),
            CredsSource::RealCreds => (accessor_cred.ruid(), accessor_cred.rgid()),
        };

        let target_cred = context.target().credentials();
        let caller_is_same = caller_uid == target_cred.euid()
            && caller_uid == target_cred.suid()
            && caller_uid == target_cred.ruid()
            && caller_gid == target_cred.egid()
            && caller_gid == target_cred.sgid()
            && caller_gid == target_cred.rgid();
        if caller_is_same || {
            let target_process = context.target().process();
            let target_user_ns = target_process.user_ns().lock();
            self.on_capable(&CapableContext::new(
                target_user_ns.as_ref(),
                context.accessor(),
                CapSet::SYS_PTRACE,
            ))
            .is_ok()
        } {
            return Ok(());
        }

        return_errno_with_message!(
            Errno::EPERM,
            "the calling process does not have the required permissions"
        );
    }
}
