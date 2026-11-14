// SPDX-License-Identifier: MPL-2.0

//! This module offers `/proc/cgroups`, the table `systemd`, `docker` and
//! `lxc-checkconfig` read to learn which cgroup controllers the kernel has.
//!
//! Reference: <https://man7.org/linux/man-pages/man7/cgroups.7.html>
//!
//! The board mounts the unified hierarchy (cgroup v2), where every controller
//! is in hierarchy `0` and the per-controller cgroup count is the number of
//! cgroups that *enable* it. This kernel enables controllers per cgroup
//! (`cgroup.subtree_control`), so the count reported here is the root's, one.

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
};

/// The controllers this kernel implements, in the order Linux lists them.
const CONTROLLERS: &[&str] = &["cpuset", "cpu", "memory", "pids"];

/// The inode at `/proc/cgroups`.
pub struct CgroupsFileOps;

impl CgroupsFileOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for CgroupsFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);

        // Linux separates the columns with tabs, and its first line is the
        // commented header.
        writeln!(
            printer,
            "#subsys_name\thierarchy\tnum_cgroups\tenabled"
        )?;
        for controller in CONTROLLERS {
            writeln!(printer, "{}\t0\t1\t1", controller)?;
        }

        Ok(printer.bytes_written())
    }
}
