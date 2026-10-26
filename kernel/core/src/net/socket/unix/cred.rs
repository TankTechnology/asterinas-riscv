// SPDX-License-Identifier: MPL-2.0

use aster_rights::{Dup, Read, ReadDupOp, ReadOp, TRights};
use aster_rights_proc::require;

use crate::{
    prelude::*,
    process::{Credentials, Gid, Pid, Uid, posix_thread::AsPosixThread},
};

pub(super) struct SocketCred<R = ReadOp> {
    pid: Pid,
    cred: Credentials<R>,
}

impl SocketCred<ReadOp> {
    pub(super) fn new_current() -> Self {
        let pid = current!().pid();
        let cred = current_thread!().as_posix_thread().unwrap().credentials();

        Self { pid, cred }
    }
}

impl SocketCred<ReadDupOp> {
    pub(super) fn new_current() -> Self {
        let pid = current!().pid();
        let cred = current_thread!()
            .as_posix_thread()
            .unwrap()
            .credentials_dup();

        Self { pid, cred }
    }
}

impl<R: TRights> SocketCred<R> {
    /// Converts to a [`CUserCred`] with the PID and the _effective_ UID/GID.
    #[require(R > Read)]
    pub(super) fn to_effective_c_cred(&self) -> CUserCred {
        CUserCred {
            pid: self.pid,
            uid: self.cred.euid(),
            gid: self.cred.egid(),
        }
    }

    /// Converts to a [`CUserCred`] with the PID and the _real_ UID/GID.
    #[require(R > Read)]
    pub(super) fn to_real_c_cred(&self) -> CUserCred {
        CUserCred {
            pid: self.pid,
            uid: self.cred.ruid(),
            gid: self.cred.rgid(),
        }
    }

    #[require(R > Read)]
    pub(super) fn groups(&self) -> Arc<[Gid]> {
        self.cred.groups().iter().cloned().collect()
    }

    #[require(R > R1)]
    pub(super) fn restrict<R1: TRights>(self) -> SocketCred<R1> {
        let Self { pid, cred } = self;
        SocketCred {
            pid,
            cred: cred.restrict(),
        }
    }

    #[require(R > Dup)]
    pub(super) fn dup(&self) -> Self {
        Self {
            pid: self.pid,
            cred: self.cred.dup(),
        }
    }
}

/// `struct ucred` in Linux.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.15/source/include/linux/socket.h#L183>.
#[repr(C)]
#[derive(Clone, Copy, Debug, Eq, PartialEq, Pod)]
pub(crate) struct CUserCred {
    pid: Pid,
    uid: Uid,
    gid: Gid,
}

impl CUserCred {
    /// Returns whether this `SCM_CREDENTIALS` claim from a sender is one the
    /// kernel may accept.
    ///
    /// Linux compares the claimed uid/gid with the sender's current ones and
    /// the claimed pid with the sender's pid as seen from its own PID
    /// namespace (`task_tgid_vnr`).  A pid of zero asks the kernel to fill the
    /// value in.
    ///
    /// Asterinas does not implement PID namespaces yet: `CLONE_NEWPID` is
    /// accepted and ignored.  Sandboxed processes (Firefox's content
    /// processes among them) therefore still believe they are pid 1 and send
    /// that value; accept it so that such IPC keeps working, while a foreign
    /// pid other than 0 or 1 is still rejected.
    pub(in crate::net) fn is_acceptable_sender_claim(&self, current: &Self) -> bool {
        if self.uid != current.uid && self.uid != Uid::INVALID {
            return false;
        }
        if self.gid != current.gid && self.gid != Gid::INVALID {
            return false;
        }
        self.pid == 0 || self.pid == 1 || self.pid == current.pid
    }

    pub(in crate::net) const fn new_invalid() -> Self {
        Self {
            pid: 0,
            uid: Uid::INVALID,
            gid: Gid::INVALID,
        }
    }

    pub(in crate::net) const fn new_overflow() -> Self {
        Self {
            pid: 0,
            uid: Uid::OVERFLOW,
            gid: Gid::OVERFLOW,
        }
    }
}
