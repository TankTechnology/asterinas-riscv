// SPDX-License-Identifier: MPL-2.0

//! This module offers `/proc/net` file support, which is a symbolic link to
//! the network statistics of the current process.
//!
//! Reference: <https://man7.org/linux/man-pages/man5/proc_net.5.html>

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcSym, ProcSymOps},
        vfs::inode::{Inode, SymbolicLink},
    },
    prelude::*,
};

/// Represents the inode at `/proc/net`.
pub(super) struct NetSymOps;

impl NetSymOps {
    pub(super) fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference: <https://elixir.bootlin.com/linux/v6.16.5/source/fs/proc/net.c#L25>
        ProcSym::new(Self, parent, mkmod!(a+rwx))
    }
}

impl ProcSymOps for NetSymOps {
    fn read_link(&self) -> Result<SymbolicLink> {
        Ok(SymbolicLink::Plain("self/net".into()))
    }
}
