// SPDX-License-Identifier: MPL-2.0

//! This module offers `/proc/kallsyms`, the kernel's symbol table.
//!
//! Reference: <https://man7.org/linux/man-pages/man5/proc_kallsyms.5.html>
//!
//! The table itself is filled in after linking (see
//! `tools/riscv/ksym/kallsyms.py`); this file is only the file. An image built
//! without the tool has an empty table, and the file reads as empty.

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
    trace::kallsyms,
};

/// The inode at `/proc/kallsyms`.
pub struct KallsymsFileOps;

impl KallsymsFileOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for KallsymsFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        kallsyms::render_from(offset, writer)
    }
}
