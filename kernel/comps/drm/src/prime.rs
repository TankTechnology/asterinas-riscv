// SPDX-License-Identifier: MPL-2.0

//! PRIME / dma-buf descriptors for GEM objects.
//!
//! `DRM_IOCTL_PRIME_HANDLE_TO_FD` turns a GEM handle into a dma-buf
//! descriptor; `DRM_IOCTL_PRIME_FD_TO_HANDLE` imports such a descriptor into
//! another DRM file's handle space. The descriptor can be passed between
//! processes with `SCM_RIGHTS`, which is what makes it useful to a compositor
//! or a DRI3 client.
//!
//! The descriptor is an anonymous file whose per-open object is a
//! [`DmaBufFile`]; `aster_core::fs::file::create_anon_file` gives the DRM core
//! a way to create such a descriptor without owning a device node for it.

use alloc::{boxed::Box, sync::Arc};

use aster_core::{
    events::IoEvents,
    fs::{
        file::{Mappable, MappableObject, MappedObject, PerOpenFileOps, StatusFlags, SyncMode},
        vfs::{
            inode::FileOps,
            path::Path,
        },
    },
    prelude::*,
    process::signal::{PollHandle, Pollable},
    util::ioctl::RawIoctl,
    vm::vmar::{FileMmapRequest, MapHandle},
};
use ostd::mm::{PAGE_SIZE, VmReader, VmWriter};

use crate::{gem::object::DrmGemObject, minor::DrmMinor};

/// The anonymous-file name shown for a dma-buf descriptor in `/proc`.
pub(crate) const DRM_BUF_FILE_NAME: &str = "anon_inode:[drm-buf]";

/// A dma-buf descriptor naming one GEM object.
///
/// The descriptor holds the GEM object directly, so the buffer stays alive for
/// as long as any process keeps the descriptor open, even after the exporting
/// DRM file closed its handle.
#[derive(Debug)]
pub(crate) struct DmaBufFile {
    object: Arc<DrmGemObject>,
    /// The DRM device that exported the object.
    ///
    /// Importing checks this so a descriptor cannot be imported into an
    /// unrelated device's handle space. (Cross-device import would need a
    /// real dma-buf attachment layer, which this kernel does not have yet.)
    minor: Arc<DrmMinor>,
}

impl DmaBufFile {
    pub(crate) fn new(object: Arc<DrmGemObject>, minor: Arc<DrmMinor>) -> Self {
        Self { object, minor }
    }

    pub(crate) fn object(&self) -> Arc<DrmGemObject> {
        self.object.clone()
    }

    pub(crate) fn minor(&self) -> Arc<DrmMinor> {
        self.minor.clone()
    }

    /// Returns the size reported for the descriptor.
    pub(crate) fn size(&self) -> usize {
        self.object.size()
    }
}

impl Pollable for DmaBufFile {
    fn poll(&self, _mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        // A dma-buf is memory, not a stream: there is no readiness to wait
        // for, and reporting `IN | OUT` would make pollers spin.
        IoEvents::empty()
    }
}

impl FileOps for DmaBufFile {
    fn read_at(
        &self,
        _offset: usize,
        _writer: &mut VmWriter,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EINVAL, "reading a dma-buf is not supported")
    }

    fn write_at(
        &self,
        _offset: usize,
        _reader: &mut VmReader,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EINVAL, "writing a dma-buf is not supported")
    }
}

impl PerOpenFileOps for DmaBufFile {
    fn check_seekable(&self) -> Result<()> {
        Ok(())
    }

    fn is_offset_aware(&self) -> bool {
        // Seeking is accepted and does not change any read/write behavior;
        // mmap offsets are absolute within the buffer.
        false
    }

    fn mappable(&self, request: FileMmapRequest) -> Result<MappableObject<'_>> {
        if !request.is_shared() {
            return_errno_with_message!(Errno::EINVAL, "private dma-buf mappings are not supported");
        }

        Ok(MappableObject::Device(self as &dyn Mappable))
    }

    fn ioctl(&self, _path: &Path, _raw_ioctl: RawIoctl) -> Result<i32> {
        return_errno_with_message!(Errno::ENOTTY, "the dma-buf descriptor supports no ioctl")
    }

    fn sync(&self, _mode: SyncMode) -> Result<()> {
        Ok(())
    }
}

impl Mappable for DmaBufFile {
    fn map(&self, offset: usize, handle: MapHandle) -> Result<Box<dyn MappedObject>> {
        let size = handle.size();
        if !offset.is_multiple_of(PAGE_SIZE) || !size.is_multiple_of(PAGE_SIZE) {
            return_errno_with_message!(Errno::EINVAL, "the dma-buf mapping is not page-aligned");
        }
        let end = offset
            .checked_add(size)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "the dma-buf mapping overflows"))?;
        if end > self.size() {
            return_errno_with_message!(
                Errno::EINVAL,
                "the dma-buf mapping exceeds the buffer"
            );
        }

        // Unlike a dumb-buffer mapping, the file offset of a dma-buf mapping
        // is the byte offset into the buffer, which is what the page-fault
        // handler of the returned object expects.
        self.object.backend().create_mapped_object(offset, size)
    }
}
