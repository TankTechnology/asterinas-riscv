// SPDX-License-Identifier: MPL-2.0

//! Virtio-gpu command submission and explicit synchronization.
//!
//! The ioctl flow is organized as decode, dependency wait, recoverable
//! reservation, irreversible device submission, and publication.

use ostd::mm::VmIo;

use super::super::{
    DriHandle,
    fence::{ExecbufferMemoryQuota, Fence, FenceFile},
    syncobj::{self, MAX_SYNCOBJ_ARRAY_ITEMS, SubmissionWait, SyncObject},
};
use crate::{
    context::current_userspace,
    fs::file::file_table::{FdFlags, FileDesc, WithFileTable},
    prelude::*,
    process::posix_thread::FileTableRefMut,
};

/// `struct drm_virtgpu_execbuffer`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub(in super::super) struct DrmVirtgpuExecbuffer {
    pub(in super::super) flags: u32,
    pub(in super::super) size: u32,
    pub(in super::super) command: u64, // void* — userspace pointer to command buffer
    pub(in super::super) bo_handles: u64, // __u32* — array of GEM handle indices
    pub(in super::super) num_bo_handles: u32,
    pub(in super::super) fence_fd: i32, // in/out fence fd
    pub(in super::super) ring_idx: u32,
    pub(in super::super) syncobj_stride: u32,
    pub(in super::super) num_in_syncobjs: u32,
    pub(in super::super) num_out_syncobjs: u32,
    pub(in super::super) in_syncobjs: u64,
    pub(in super::super) out_syncobjs: u64,
}

/// One input or output syncobj descriptor in `drm_virtgpu_execbuffer`.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
struct DrmVirtgpuExecbufferSyncobj {
    handle: u32,
    flags: u32,
    point: u64,
}

/// Maximum size in bytes of one userspace-provided virgl command stream.
const MAX_EXECBUFFER_SIZE_BYTES: usize = 16 * 1024 * 1024;
const MAX_EXECBUFFER_HANDLES: usize = 4096;

/// `VIRTGPU_EXECBUF_FENCE_FD_IN` — `fence_fd` names a sync fence that must
/// complete before the command is submitted.
const VIRTGPU_EXECBUF_FENCE_FD_IN: u32 = 0x01;

/// `VIRTGPU_EXECBUF_FENCE_FD_OUT` — the caller requests an out-fence (a pollable
/// fd signaling when the submitted command completes).
const VIRTGPU_EXECBUF_FENCE_FD_OUT: u32 = 0x02;

/// Reset an input syncobj after it has been consumed by the submission.
const VIRTGPU_EXECBUF_SYNCOBJ_RESET: u32 = 0x01;

/// Submits a virgl command stream to the host.
///
/// Mesa encodes GL commands in a virgl command buffer and submits them through
/// this ioctl.
/// Every submission receives a persistent fence and is queued without waiting
/// for rendering to finish.
/// Setting `VIRTGPU_EXECBUF_FENCE_FD_OUT` controls whether that fence is also
/// returned as a [`FenceFile`].
/// The file becomes readable when the control-queue IRQ observes the fenced
/// response; a fast device may signal it before the ioctl copies out the fd.
pub(in super::super) fn virtgpu_execbuffer(
    handle: &DriHandle,
    cmd: crate::util::ioctl::Ioctl<
        b'd',
        0x42,
        true,
        crate::util::ioctl::InOutData<DrmVirtgpuExecbuffer>,
    >,
    file_table: &mut FileTableRefMut,
) -> Option<Result<i32>> {
    Some((|| -> Result<i32> {
        let decoded = DecodedExecbuffer::read(handle, cmd.read()?)?;
        decoded.wait_for_dependencies(file_table)?;
        let prepared = decoded.prepare()?;
        let mut response = ExecbufferResponse::prepare(prepared.request, file_table)?;
        prepared.submit(handle, &mut response)?;
        response.write(cmd)?;
        Ok(0)
    })())
}

/// An execbuffer whose userspace arrays and command stream have been copied.
///
/// Holding this value is still fully reversible: no device command or syncobj
/// publication has occurred.
struct DecodedExecbuffer {
    request: DrmVirtgpuExecbuffer,
    command: Vec<u8>,
    command_quota: ExecbufferMemoryQuota,
    input_syncobjs: Vec<ExecbufferSyncobj>,
    output_syncobjs: Vec<ExecbufferSyncobj>,
}

impl DecodedExecbuffer {
    fn read(handle: &DriHandle, request: DrmVirtgpuExecbuffer) -> Result<Self> {
        if request.flags & !(VIRTGPU_EXECBUF_FENCE_FD_IN | VIRTGPU_EXECBUF_FENCE_FD_OUT) != 0
            || request.ring_idx != 0
        {
            return_errno_with_message!(Errno::EINVAL, "unsupported execbuffer synchronization");
        }

        let input_syncobjs = parse_execbuffer_syncobjs(
            handle,
            request.in_syncobjs,
            request.num_in_syncobjs,
            request.syncobj_stride,
            SyncobjDirection::Input,
        )?;
        let output_syncobjs = parse_execbuffer_syncobjs(
            handle,
            request.out_syncobjs,
            request.num_out_syncobjs,
            request.syncobj_stride,
            SyncobjDirection::Output,
        )?;

        if request.size == 0 || request.command == 0 {
            return_errno_with_message!(Errno::EINVAL, "empty command buffer");
        }
        let command_size_bytes = request.size as usize;
        if command_size_bytes > MAX_EXECBUFFER_SIZE_BYTES {
            return_errno_with_message!(Errno::EINVAL, "command buffer is too large");
        }

        // Mesa's atomic path relies on copying the stream before an input
        // fence wait. Retaining the quota in this typed stage bounds that
        // compatibility-required memory until the device ticket is released.
        let command_quota = ExecbufferMemoryQuota::reserve(command_size_bytes)?;
        let mut command = Vec::new();
        command
            .try_reserve_exact(command_size_bytes)
            .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate command buffer"))?;
        command.resize(command_size_bytes, 0);
        current_userspace!().read_bytes(request.command as usize, &mut command)?;

        Ok(Self {
            request,
            command,
            command_quota,
            input_syncobjs,
            output_syncobjs,
        })
    }

    fn wait_for_dependencies(&self, file_table: &mut FileTableRefMut) -> Result<()> {
        // Read the shared in/out field before an output fd can replace it.
        if self.request.flags & VIRTGPU_EXECBUF_FENCE_FD_IN != 0 {
            let fd = FileDesc::try_from(self.request.fence_fd)?;
            let file = file_table
                .read_with(|table| table.get_file(fd).cloned())
                .map_err(|_| Error::new(Errno::EBADF))?;
            let fence_file = file.downcast_ref::<FenceFile>().ok_or_else(|| {
                Error::with_message(Errno::EINVAL, "execbuffer input fd is not a sync fence")
            })?;
            fence_file.fence().wait_for_dependency();
        }
        for descriptor in &self.input_syncobjs {
            descriptor
                .syncobj
                .wait_for_fence(descriptor.point, SubmissionWait::Immediate)?
                .wait_for_dependency();
        }
        Ok(())
    }

    fn prepare(self) -> Result<PreparedExecbuffer> {
        let mut output_specs = Vec::new();
        output_specs
            .try_reserve_exact(self.output_syncobjs.len())
            .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot reserve output syncobjs"))?;
        for descriptor in &self.output_syncobjs {
            output_specs.push((descriptor.syncobj.clone(), descriptor.point));
        }
        let output_publications = syncobj::reserve_publication_batch(&output_specs)?;
        let object_handles = read_object_handles(self.request)?;

        Ok(PreparedExecbuffer {
            request: self.request,
            command: self.command,
            command_quota: self.command_quota,
            input_syncobjs: self.input_syncobjs,
            output_publications,
            object_handles,
        })
    }
}

/// A submission with all recoverable publication and userspace state reserved.
struct PreparedExecbuffer {
    request: DrmVirtgpuExecbuffer,
    command: Vec<u8>,
    command_quota: ExecbufferMemoryQuota,
    input_syncobjs: Vec<ExecbufferSyncobj>,
    output_publications: Vec<syncobj::SyncobjPublication>,
    object_handles: Vec<u32>,
}

impl PreparedExecbuffer {
    fn submit(self, handle: &DriHandle, response: &mut ExecbufferResponse<'_, '_>) -> Result<()> {
        let Self {
            request,
            command,
            command_quota,
            input_syncobjs,
            output_publications,
            object_handles,
        } = self;

        // This lock hierarchy serializes GEM final release, resource capture,
        // control-queue order, and syncobj publication in that order.
        let resource_creation = handle.gpu_manager.resource_creation.lock();
        let resource_transaction = handle.gpu_manager.exec_resource_transaction.lock();
        let (object_ids, resource_ids) = resolve_resources(handle, &object_handles)?;
        handle
            .gpu_manager
            .reserve_resource_fence_associations(&object_ids)?;
        let context_id = handle.attach_resources_to_context(&resource_ids)?;
        let fence_id = handle.gpu_manager.allocate_fence_id()?;
        let fence = response.fence().clone();
        let ticket = handle
            .gpu_manager
            .gpu
            .submit_3d_fenced_async(context_id, request.size, &command, fence_id, fence.clone())
            .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu error"))?;

        // No fallible operation is allowed after this point until every
        // prepared publication owns the submitted fence.
        fence.attach(ticket, command_quota);
        response.mark_submitted();
        drop(command);
        handle
            .gpu_manager
            .associate_resource_fence(&object_ids, &fence);
        drop(resource_creation);

        // Reset consumed inputs before publishing outputs. An object may appear
        // in both lists, and its newly published output must survive RESET.
        for descriptor in &input_syncobjs {
            if descriptor.should_reset {
                descriptor.syncobj.clear_fence();
            }
        }
        for publication in output_publications {
            publication.publish(fence.clone());
        }
        drop(resource_transaction);
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ExecbufferResponseState {
    Prepared,
    Submitted,
    Published,
}

/// Owns the preinstalled output fd and rolls it back on any later failure.
struct ExecbufferResponse<'borrow, 'table> {
    file_table: &'borrow mut FileTableRefMut<'table>,
    fence: Arc<Fence>,
    response: DrmVirtgpuExecbuffer,
    installed_fence_fd: Option<(FileDesc, Arc<dyn crate::fs::file::FileLike>)>,
    state: ExecbufferResponseState,
}

impl<'borrow, 'table> ExecbufferResponse<'borrow, 'table> {
    fn prepare(
        mut response: DrmVirtgpuExecbuffer,
        file_table: &'borrow mut FileTableRefMut<'table>,
    ) -> Result<Self> {
        let fence = Arc::try_new(Fence::new())
            .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate execbuffer fence"))?;
        let installed_fence_fd = if response.flags & VIRTGPU_EXECBUF_FENCE_FD_OUT != 0 {
            let fence_file = Arc::try_new(FenceFile::new(fence.clone())).map_err(|_| {
                Error::with_message(Errno::ENOMEM, "cannot allocate execbuffer fence file")
            })?;
            let file: Arc<dyn crate::fs::file::FileLike> = fence_file;
            let fd = file_table
                .unwrap()
                .write()
                .insert(file.clone(), FdFlags::CLOEXEC);
            response.fence_fd = u32::from(fd) as i32;
            Some((fd, file))
        } else {
            None
        };

        Ok(Self {
            file_table,
            fence,
            response,
            installed_fence_fd,
            state: ExecbufferResponseState::Prepared,
        })
    }

    fn fence(&self) -> &Arc<Fence> {
        &self.fence
    }

    fn mark_submitted(&mut self) {
        debug_assert_eq!(self.state, ExecbufferResponseState::Prepared);
        self.state = ExecbufferResponseState::Submitted;
    }

    fn write(
        mut self,
        cmd: crate::util::ioctl::Ioctl<
            b'd',
            0x42,
            true,
            crate::util::ioctl::InOutData<DrmVirtgpuExecbuffer>,
        >,
    ) -> Result<()> {
        cmd.write(&self.response)?;
        self.state = ExecbufferResponseState::Published;
        Ok(())
    }
}

impl Drop for ExecbufferResponse<'_, '_> {
    fn drop(&mut self) {
        if self.state == ExecbufferResponseState::Prepared {
            self.fence.signal_failure();
        }
        if self.state != ExecbufferResponseState::Published
            && let Some((fd, file)) = self.installed_fence_fd.take()
        {
            let closed = self
                .file_table
                .unwrap()
                .write()
                .close_file_if_same(fd, &file);
            drop(closed);
        }
    }
}

fn read_object_handles(request: DrmVirtgpuExecbuffer) -> Result<Vec<u32>> {
    if request.num_bo_handles == 0 {
        return Ok(Vec::new());
    }
    if request.bo_handles == 0 {
        return_errno_with_message!(Errno::EINVAL, "missing execbuffer handle list");
    }

    let handle_count = request.num_bo_handles as usize;
    if handle_count > MAX_EXECBUFFER_HANDLES {
        return_errno_with_message!(Errno::EINVAL, "too many execbuffer handles");
    }
    let byte_count = handle_count
        .checked_mul(size_of::<u32>())
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "handle list overflows"))?;
    let mut raw = Vec::new();
    raw.try_reserve_exact(byte_count)
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate handle list"))?;
    raw.resize(byte_count, 0);
    current_userspace!().read_bytes(request.bo_handles as usize, &mut raw)?;

    let mut handles = Vec::new();
    handles
        .try_reserve_exact(handle_count)
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot decode handle list"))?;
    for bytes in raw.as_chunks::<4>().0 {
        handles.push(u32::from_le_bytes(*bytes));
    }
    Ok(handles)
}

fn resolve_resources(handle: &DriHandle, object_handles: &[u32]) -> Result<(Vec<u32>, Vec<u32>)> {
    let mut object_ids = Vec::new();
    object_ids
        .try_reserve_exact(object_handles.len())
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot reserve execbuffer object ids"))?;
    {
        let inner = handle.inner.lock();
        let objects = handle.gpu_manager.gem_objects.lock();
        for object_handle in object_handles {
            let Some(object_id) = inner.handles.get(object_handle).copied() else {
                return_errno_with_message!(Errno::EINVAL, "unknown GEM handle in execbuffer");
            };
            if !objects.contains_key(&object_id) {
                return_errno_with_message!(Errno::EINVAL, "stale GEM handle in execbuffer");
            }
            object_ids.push(object_id);
        }
    }
    object_ids.sort_unstable();
    object_ids.dedup();

    let mut resource_ids = Vec::new();
    resource_ids
        .try_reserve_exact(object_ids.len())
        .map_err(|_| {
            Error::with_message(Errno::ENOMEM, "cannot reserve execbuffer resource ids")
        })?;
    for object_id in &object_ids {
        if let Some(resource_id) = handle.gpu_manager.live_gem_resource(*object_id) {
            resource_ids.push(resource_id);
        }
    }
    Ok((object_ids, resource_ids))
}

struct ExecbufferSyncobj {
    syncobj: Arc<SyncObject>,
    point: u64,
    should_reset: bool,
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum SyncobjDirection {
    Input,
    Output,
}

fn parse_execbuffer_syncobjs(
    handle: &DriHandle,
    pointer: u64,
    count: u32,
    stride: u32,
    direction: SyncobjDirection,
) -> Result<Vec<ExecbufferSyncobj>> {
    let count = count as usize;
    if count == 0 {
        return Ok(Vec::new());
    }
    if count > MAX_SYNCOBJ_ARRAY_ITEMS {
        return_errno_with_message!(Errno::EINVAL, "too many execbuffer syncobjs");
    }
    // `handle` and `flags` must always be present. Older userspace may use a
    // shorter descriptor without the optional timeline point.
    if pointer == 0 || stride < 8 {
        return_errno_with_message!(Errno::EINVAL, "invalid execbuffer syncobj array");
    }

    let stride = stride as usize;
    let copy_len = stride.min(size_of::<DrmVirtgpuExecbufferSyncobj>());
    let mut wire = Vec::new();
    wire.try_reserve_exact(count)
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate syncobj descriptors"))?;
    for index in 0..count {
        let offset = index
            .checked_mul(stride)
            .and_then(|offset| pointer.checked_add(offset as u64))
            .ok_or_else(|| {
                Error::with_message(Errno::EFAULT, "syncobj descriptor address overflows")
            })?;
        let address = usize::try_from(offset).map_err(|_| {
            Error::with_message(Errno::EFAULT, "syncobj descriptor address overflows")
        })?;
        let mut bytes = [0u8; size_of::<DrmVirtgpuExecbufferSyncobj>()];
        current_userspace!().read_bytes(address, &mut bytes[..copy_len])?;
        wire.push(DrmVirtgpuExecbufferSyncobj {
            handle: u32::from_le_bytes(bytes[0..4].try_into().unwrap()),
            flags: u32::from_le_bytes(bytes[4..8].try_into().unwrap()),
            point: u64::from_le_bytes(bytes[8..16].try_into().unwrap()),
        });
    }

    let mut handles = Vec::new();
    handles
        .try_reserve_exact(wire.len())
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate syncobj handles"))?;
    for descriptor in &wire {
        handles.push(descriptor.handle);
    }
    let syncobjs = syncobj::lookup_syncobjs(handle, &handles)?;
    let mut descriptors = Vec::new();
    descriptors
        .try_reserve_exact(wire.len())
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot decode syncobj descriptors"))?;
    for (descriptor, syncobj) in wire.into_iter().zip(syncobjs) {
        if (direction == SyncobjDirection::Input
            && descriptor.flags & !VIRTGPU_EXECBUF_SYNCOBJ_RESET != 0)
            || (direction == SyncobjDirection::Output && descriptor.flags != 0)
        {
            return_errno_with_message!(Errno::EINVAL, "unknown execbuffer syncobj flags");
        }
        descriptors.push(ExecbufferSyncobj {
            syncobj,
            point: descriptor.point,
            should_reset: direction == SyncobjDirection::Input
                && descriptor.flags & VIRTGPU_EXECBUF_SYNCOBJ_RESET != 0,
        });
    }
    Ok(descriptors)
}
