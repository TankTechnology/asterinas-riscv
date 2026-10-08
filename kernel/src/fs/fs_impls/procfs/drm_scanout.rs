// SPDX-License-Identifier: MPL-2.0

//! Read-on-demand firmware DRM scanout counters for bounded display experiments.

use aster_util::printer::VmPrinter;

use crate::{
    device,
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
};

pub(super) struct DrmScanoutFileOps;

impl DrmScanoutFileOps {
    pub(super) fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for DrmScanoutFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        let line =
            device::drm_scanout_snapshot_line().unwrap_or_else(|| "unavailable\n".to_string());
        write!(printer, "{line}")?;
        Ok(printer.bytes_written())
    }
}
