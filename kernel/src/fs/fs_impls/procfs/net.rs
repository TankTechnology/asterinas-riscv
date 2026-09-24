// SPDX-License-Identifier: MPL-2.0

//! The `/proc/net` symlink to the caller's network namespace view.

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcSym, ProcSymOps},
        vfs::inode::{Inode, SymbolicLink},
    },
    prelude::*,
};

pub(super) struct NetSymOps;

impl NetSymOps {
    pub(super) fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcSym::new(Self, parent, mkmod!(a+rwx))
    }
}

impl ProcSymOps for NetSymOps {
    fn read_link(&self) -> Result<SymbolicLink> {
        Ok(SymbolicLink::Plain("self/net".into()))
    }
}
