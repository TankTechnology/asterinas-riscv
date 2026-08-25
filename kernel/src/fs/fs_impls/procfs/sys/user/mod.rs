// SPDX-License-Identifier: MPL-2.0

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::{InodeType, mkmod},
        procfs::{
            ProcDir, StaticEntry,
            sys::user::max_user_namespaces::MaxUserNamespacesFileOps,
            template::{
                ProcDirOps, ReaddirEntry, listed_entries_from_table, lookup_child_from_table,
                visit_listed_entries,
            },
        },
        vfs::inode::Inode,
    },
    prelude::*,
};

mod max_user_namespaces;

/// Represents the inode at `/proc/sys/user`.
pub struct UserDirOps;

impl UserDirOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcDir::new(Self, parent, mkmod!(a+rx))
    }

    const STATIC_ENTRIES: &'static [StaticEntry] = &[(
        "max_user_namespaces",
        InodeType::File,
        MaxUserNamespacesFileOps::new_inode,
    )];
}

impl ProcDirOps for UserDirOps {
    fn lookup_child(&self, this_dir: &ProcDir<Self>, name: &str) -> Result<Arc<dyn Inode>> {
        if let Some(child) = lookup_child_from_table(name, Self::STATIC_ENTRIES, |f| {
            (f)(this_dir.this_weak().clone())
        }) {
            return Ok(child);
        }

        return_errno_with_message!(Errno::ENOENT, "the file does not exist");
    }

    fn visit_entries_from_offset<'a, F>(&'a self, offset: usize, visit_fn: F) -> Result<()>
    where
        F: FnMut(ReaddirEntry<'a>) -> Result<()>,
    {
        visit_listed_entries(
            offset,
            listed_entries_from_table(Self::STATIC_ENTRIES),
            visit_fn,
        )
    }
}
