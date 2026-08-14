// SPDX-License-Identifier: MPL-2.0

use core::sync::atomic::{AtomicU32, Ordering};

use super::SyscallReturn;
use crate::{prelude::*, syscall::constants::MAX_FILENAME_LEN};

/// A minimal keyring serial allocator.
///
/// Asterinas has no keyring subsystem: keys are not retained, so this allocator
/// only hands out fresh, monotonically-increasing serial numbers. That is enough
/// to satisfy `libkeyutils`-style lookups, which only need a non-zero keyring
/// serial from `keyctl(KEYCTL_GET_KEYRING_ID)` and never dereference it.
static NEXT_KEY_SERIAL: AtomicU32 = AtomicU32::new(1);

/// The serial of the (single, implicit) session keyring.
static SESSION_KEYRING_SERIAL: AtomicU32 = AtomicU32::new(0);

fn alloc_serial() -> u32 {
    NEXT_KEY_SERIAL.fetch_add(1, Ordering::Relaxed)
}

/// Returns the session keyring serial, allocating one on first use.
fn session_keyring_serial() -> u32 {
    let serial = SESSION_KEYRING_SERIAL.load(Ordering::Relaxed);
    if serial != 0 {
        return serial;
    }
    let candidate = alloc_serial();
    match SESSION_KEYRING_SERIAL.compare_exchange(0, candidate, Ordering::Relaxed, Ordering::Relaxed)
    {
        Ok(_) => candidate,
        Err(existing) => existing,
    }
}

// --- `keyctl` command numbers (`linux/keyctl.h`) ---
const KEYCTL_GET_KEYRING_ID: i32 = 0;
const KEYCTL_JOIN_SESSION_KEYRING: i32 = 1;
const KEYCTL_UPDATE: i32 = 2;
const KEYCTL_REVOKE: i32 = 3;
const KEYCTL_CHOWN: i32 = 4;
const KEYCTL_SETPERM: i32 = 5;
const KEYCTL_CLEAR: i32 = 7;
const KEYCTL_LINK: i32 = 8;
const KEYCTL_UNLINK: i32 = 9;
const KEYCTL_SEARCH: i32 = 10;
const KEYCTL_SESSION_TO_PARENT: i32 = 18;

pub fn sys_add_key(
    type_addr: Vaddr,
    desc_addr: Vaddr,
    payload_addr: Vaddr,
    plen: usize,
    keyring: i32,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let key_type = ctx.user_space().read_cstring(type_addr, MAX_FILENAME_LEN)?;
    let description = ctx.user_space().read_cstring(desc_addr, MAX_FILENAME_LEN)?;
    debug!(
        "add_key type={:?} desc={:?} plen={} keyring={}",
        key_type, description, plen, keyring
    );

    // The payload pointer is deliberately not dereferenced: no keyring storage
    // exists, so the payload is never retained. The key serial is all callers
    // can observe from this stub.
    let _ = (payload_addr, plen);

    let serial = alloc_serial();
    Ok(SyscallReturn::Return(serial as isize))
}

pub fn sys_request_key(
    type_addr: Vaddr,
    desc_addr: Vaddr,
    callout_info: Vaddr,
    dest_ringid: i32,
    ctx: &Context,
) -> Result<SyscallReturn> {
    let key_type = ctx.user_space().read_cstring(type_addr, MAX_FILENAME_LEN)?;
    let description = ctx.user_space().read_cstring(desc_addr, MAX_FILENAME_LEN)?;
    debug!(
        "request_key type={:?} desc={:?} callout_info={:#x} dest_ringid={}",
        key_type, description, callout_info, dest_ringid
    );

    // No keys are ever retained, so a lookup can never succeed.
    return_errno_with_message!(Errno::ENOKEY, "no matching key");
}

pub fn sys_keyctl(
    option: i32,
    arg2: u64,
    arg3: u64,
    arg4: u64,
    arg5: u64,
    _ctx: &Context,
) -> Result<SyscallReturn> {
    debug!(
        "keyctl option={} arg2={} arg3={} arg4={} arg5={}",
        option, arg2, arg3, arg4, arg5
    );

    match option {
        KEYCTL_GET_KEYRING_ID => Ok(SyscallReturn::Return(session_keyring_serial() as isize)),
        KEYCTL_JOIN_SESSION_KEYRING => {
            Ok(SyscallReturn::Return(session_keyring_serial() as isize))
        }
        KEYCTL_REVOKE => {
            // Keys are never retained, so there is nothing to revoke.
            Ok(SyscallReturn::Return(0))
        }
        // systemd's service-exec path (setup_keyring) walks the session keyring
        // for every spawned service. Since no keys are retained, these
        // operations are no-ops that report success: they must NOT return
        // EOPNOTSUPP or the service is aborted at the KEYRING exec step.
        KEYCTL_UPDATE | KEYCTL_CHOWN | KEYCTL_SETPERM | KEYCTL_CLEAR | KEYCTL_LINK
        | KEYCTL_UNLINK | KEYCTL_SESSION_TO_PARENT => Ok(SyscallReturn::Return(0)),
        KEYCTL_SEARCH => {
            // No keys are ever retained, so a search can never succeed.
            return_errno_with_message!(Errno::ENOKEY, "no matching key");
        }
        _ => return_errno_with_message!(Errno::EOPNOTSUPP, "keyctl command is not supported"),
    }
}
