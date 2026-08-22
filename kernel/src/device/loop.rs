// SPDX-License-Identifier: MPL-2.0

//! Loop device subsystem.
//!
//! Provides `/dev/loopN` block devices backed by regular files, and
//! `/dev/loop-control` for dynamic allocation. Each loop device also exposes
//! the matching sysfs attributes under `/sys/block/loopN/` (`ro`, `size`) and
//! `/sys/block/loopN/loop/` (`backing_file`, `partscan`, `autoclear`,
//! `sizelimit`), as LTP's `ioctl_loop*` tests read them.

use core::sync::atomic::{AtomicU32, AtomicU64, AtomicUsize, Ordering};

use aster_block::{
    BlockDevice, BlockDeviceMeta, SECTOR_SIZE,
    bio::{BioEnqueueError, BioStatus, BioType, SubmittedBio},
};
use aster_systree::{
    AttrLessBranchNodeFields, BranchNodeFields, Error as SysTreeError, NormalNodeFields,
    Result as SysTreeResult, SysAttrSetBuilder, SysObj, SysPerms, SysStr, inherit_sys_branch_node,
    inherit_sys_leaf_node,
};
use aster_util::printer::VmPrinter;
use device_id::{DeviceId, MinorId};
use inherit_methods_macro::inherit_methods;
use ostd::mm::VmIo;
use spin::Once;

use crate::{
    context::current_userspace,
    device::{
        Device, DeviceType, DevtmpfsInodeMeta,
        registry::char::{self, MajorIdOwner as CharMajorIdOwner},
    },
    events::IoEvents,
    fs::{
        file::{
            FileLike, InodeHandle, PerOpenFileOps, SeekFrom, SettableStatusFlags, StatusFlags,
            file_table::WithFileTable,
        },
        vfs::{inode::FileOps, path::Path},
    },
    prelude::*,
    process::{
        posix_thread::FileTableRefMut,
        signal::{PollHandle, Pollable},
    },
    util::ioctl::{RawIoctl, dispatch_ioctl},
};

/// Number of loop devices to pre-create.
const NUM_LOOP_DEVICES: u32 = 8;

/// Linux magic byte for loop ioctls is 'L' = 0x4C.
pub(crate) const LOOP_MAGIC: u8 = 0x4C;

/// ioctl command numbers (from linux/loop.h).
pub(crate) const LOOP_SET_FD: u32 = 0x4C00;
pub(crate) const LOOP_CLR_FD: u32 = 0x4C01;
pub(crate) const LOOP_SET_STATUS: u32 = 0x4C02;
pub(crate) const LOOP_GET_STATUS: u32 = 0x4C03;
const LOOP_SET_STATUS64: u32 = 0x4C04;
const LOOP_GET_STATUS64: u32 = 0x4C05;
const LOOP_CHANGE_FD: u32 = 0x4C06;
const LOOP_SET_CAPACITY: u32 = 0x4C07;
const LOOP_SET_BLOCK_SIZE: u32 = 0x4C09;
const LOOP_CONFIGURE: u32 = 0x4C0A;
const LOOP_CTL_GET_FREE: u32 = 0x4C82;

/// `lo_flags` bits (from linux/loop.h).
const LO_FLAGS_READ_ONLY: u32 = 1;
const LO_FLAGS_AUTOCLEAR: u32 = 4;
const LO_FLAGS_PARTSCAN: u32 = 8;
const LO_FLAGS_DIRECT_IO: u32 = 16;

/// Flags that `LOOP_SET_STATUS`/`LOOP_SET_STATUS64` may set or clear.
const LOOP_SET_STATUS_SETTABLE_FLAGS: u32 = LO_FLAGS_AUTOCLEAR | LO_FLAGS_PARTSCAN;
/// Flags that `LOOP_SET_STATUS`/`LOOP_SET_STATUS64` may clear.
const LOOP_SET_STATUS_CLEARABLE_FLAGS: u32 = LO_FLAGS_AUTOCLEAR;
/// Flags that `LOOP_CONFIGURE` may set.
const LOOP_CONFIGURE_SETTABLE_FLAGS: u32 =
    LO_FLAGS_READ_ONLY | LO_FLAGS_AUTOCLEAR | LO_FLAGS_PARTSCAN | LO_FLAGS_DIRECT_IO;

/// Field offsets in `struct loop_info` (the legacy ioctl ABI, 160 bytes on
/// 64-bit; see `linux/loop.h`).
mod loop_info {
    pub(super) const SIZE: usize = 160;
    pub(super) const LO_FLAGS: usize = 32;
    pub(super) const LO_NAME: usize = 36;
}

/// Field offsets in `struct loop_info64` (232 bytes on 64-bit).
mod loop_info64 {
    pub(super) const SIZE: usize = 232;
    pub(super) const LO_SIZELIMIT: usize = 32;
    pub(super) const LO_FLAGS: usize = 52;
    pub(super) const LO_FILE_NAME: usize = 56;
}

/// Field offsets in `struct loop_config` (304 bytes on 64-bit).
mod loop_config {
    pub(super) const SIZE: usize = 304;
    pub(super) const FD: usize = 0;
    pub(super) const BLOCK_SIZE: usize = 4;
    pub(super) const INFO: usize = 8;
}

/// Size of the `lo_name`/`lo_file_name` field in `struct loop_info{,64}`.
const LO_NAME_SIZE: usize = 64;

/// The maximum block size accepted by `LOOP_SET_BLOCK_SIZE`/`LOOP_CONFIGURE`
/// (one page, matching Linux's `BLOCK_MAX_BLOCK_SIZE`).
const MAX_LOOP_BLOCK_SIZE: usize = 4096;

/// A loop block device backed by a regular file.
pub struct LoopDevice {
    id: DeviceId,
    name: String,
    backing_file: Mutex<Option<Arc<dyn FileLike>>>,
    /// The absolute path of the backing file, shown via the sysfs
    /// `loop/backing_file` attribute.
    backing_path: Mutex<Option<String>>,
    /// The size of the backing file in bytes.
    backing_size: AtomicUsize,
    /// The `lo_sizelimit` value in bytes; 0 means no limit.
    sizelimit: AtomicU64,
    lo_flags: AtomicU32,
    lo_name: Mutex<[u8; LO_NAME_SIZE]>,
}

impl Debug for LoopDevice {
    fn fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result {
        f.debug_struct("LoopDevice")
            .field("id", &self.id)
            .field("name", &self.name)
            .field("backing_size", &self.backing_size)
            .finish()
    }
}

impl LoopDevice {
    fn new(id: DeviceId, name: &str) -> Arc<Self> {
        Arc::new(Self {
            id,
            name: name.into(),
            backing_file: Mutex::new(None),
            backing_path: Mutex::new(None),
            backing_size: AtomicUsize::new(0),
            sizelimit: AtomicU64::new(0),
            lo_flags: AtomicU32::new(0),
            lo_name: Mutex::new([0u8; LO_NAME_SIZE]),
        })
    }

    /// Returns the effective size of the loop device in bytes, i.e. the
    /// backing file size clamped to `lo_sizelimit` (if set).
    fn size_bytes(&self) -> usize {
        let backing_size = self.backing_size.load(Ordering::Relaxed);
        let sizelimit = self.sizelimit.load(Ordering::Relaxed);
        if sizelimit > 0 {
            backing_size.min(sizelimit as usize)
        } else {
            backing_size
        }
    }

    fn lo_flags(&self) -> u32 {
        self.lo_flags.load(Ordering::Relaxed)
    }

    fn is_read_only(&self) -> bool {
        self.lo_flags() & LO_FLAGS_READ_ONLY != 0
    }

    fn sizelimit(&self) -> u64 {
        self.sizelimit.load(Ordering::Relaxed)
    }

    fn backing_path(&self) -> Option<String> {
        self.backing_path.lock().clone()
    }

    /// Attaches a backing file to the loop device (`LOOP_SET_FD`).
    ///
    /// Like Linux, attaching a file that is not opened for writing makes the
    /// loop device read-only.
    fn set_backing_file(&self, file: Arc<dyn FileLike>, path: Option<String>) -> Result<()> {
        let mut backing = self.backing_file.lock();
        if backing.is_some() {
            return_errno_with_message!(Errno::EBUSY, "the loop device already has a backing file");
        }

        let size = file.seek(SeekFrom::End(0))?;
        if !file.access_mode().is_writable() {
            self.lo_flags
                .fetch_or(LO_FLAGS_READ_ONLY, Ordering::Relaxed);
        }
        *backing = Some(file);
        *self.backing_path.lock() = path;
        self.backing_size.store(size, Ordering::Relaxed);
        Ok(())
    }

    fn clear_backing_file(&self) {
        *self.backing_file.lock() = None;
        *self.backing_path.lock() = None;
        self.backing_size.store(0, Ordering::Relaxed);
        self.sizelimit.store(0, Ordering::Relaxed);
        self.lo_flags.store(0, Ordering::Relaxed);
    }

    fn has_backing_file(&self) -> bool {
        self.backing_file.lock().is_some()
    }

    /// Applies `LOOP_SET_STATUS`/`LOOP_SET_STATUS64`.
    ///
    /// Only `LOOP_SET_STATUS_SETTABLE_FLAGS` may be changed, and only
    /// `LOOP_SET_STATUS_CLEARABLE_FLAGS` may be cleared. The legacy
    /// `LOOP_SET_STATUS` carries no `lo_sizelimit` field; Linux resets the
    /// limit to 0 (unlimited) in that case.
    fn set_status(&self, flags: u32, sizelimit: Option<u64>, name: Option<&[u8]>) -> Result<()> {
        if !self.has_backing_file() {
            return_errno_with_message!(Errno::ENXIO, "the loop device has no backing file");
        }

        let new_flags = (self.lo_flags() & !LOOP_SET_STATUS_CLEARABLE_FLAGS)
            | (flags & LOOP_SET_STATUS_SETTABLE_FLAGS);
        self.lo_flags.store(new_flags, Ordering::Relaxed);

        if let Some(sizelimit) = sizelimit {
            self.sizelimit.store(sizelimit, Ordering::Relaxed);
        }
        if let Some(name) = name {
            self.set_lo_name(name);
        }
        Ok(())
    }

    /// Replaces the backing file (`LOOP_CHANGE_FD`).
    ///
    /// Like Linux, this is only allowed on a bound, read-only loop device and
    /// the new backing file must have the same size as the old one.
    fn change_backing_file(&self, file: Arc<dyn FileLike>, path: Option<String>) -> Result<()> {
        let mut backing = self.backing_file.lock();
        if backing.is_none() || !self.is_read_only() {
            return_errno_with_message!(
                Errno::EINVAL,
                "LOOP_CHANGE_FD requires a bound, read-only loop device"
            );
        }

        let new_size = file.seek(SeekFrom::End(0))?;
        if new_size != self.backing_size.load(Ordering::Relaxed) {
            return_errno_with_message!(
                Errno::EINVAL,
                "the new backing file must have the same size as the old one"
            );
        }

        *backing = Some(file);
        *self.backing_path.lock() = path;
        Ok(())
    }

    /// Re-reads the device capacity from the backing file
    /// (`LOOP_SET_CAPACITY`).
    fn refresh_capacity(&self) -> Result<()> {
        let backing = self.backing_file.lock();
        let Some(file) = backing.as_ref() else {
            return_errno_with_message!(Errno::ENXIO, "the loop device has no backing file");
        };

        let size = file.seek(SeekFrom::End(0))?;
        self.backing_size.store(size, Ordering::Relaxed);
        Ok(())
    }

    /// Atomically attaches and configures the loop device (`LOOP_CONFIGURE`).
    fn configure(
        &self,
        file: Arc<dyn FileLike>,
        path: Option<String>,
        flags: u32,
        sizelimit: u64,
        name: Option<&[u8]>,
    ) -> Result<()> {
        let mut backing = self.backing_file.lock();
        if backing.is_some() {
            return_errno_with_message!(Errno::EBUSY, "the loop device already has a backing file");
        }

        let size = file.seek(SeekFrom::End(0))?;
        let mut lo_flags = flags & LOOP_CONFIGURE_SETTABLE_FLAGS;
        if !file.access_mode().is_writable() {
            lo_flags |= LO_FLAGS_READ_ONLY;
        }

        *backing = Some(file);
        *self.backing_path.lock() = path;
        self.backing_size.store(size, Ordering::Relaxed);
        self.sizelimit.store(sizelimit, Ordering::Relaxed);
        self.lo_flags.store(lo_flags, Ordering::Relaxed);
        if let Some(name) = name {
            self.set_lo_name(name);
        }
        Ok(())
    }

    fn set_lo_name(&self, name: &[u8]) {
        let mut lo_name = self.lo_name.lock();
        let len = name.len().min(LO_NAME_SIZE - 1);
        lo_name[..len].copy_from_slice(&name[..len]);
        lo_name[len] = 0;
    }

    fn lo_name(&self) -> [u8; LO_NAME_SIZE] {
        *self.lo_name.lock()
    }
}

impl BlockDevice for LoopDevice {
    fn enqueue(&self, bio: SubmittedBio) -> Result<(), BioEnqueueError> {
        let type_ = bio.type_();
        let mut sid_range = bio.sid_range().clone();
        let sid_offset = bio.sid_offset();
        sid_range.start = sid_range.start + sid_offset;
        sid_range.end = sid_range.end + sid_offset;

        let backing = self.backing_file.lock();
        let Some(ref backing_file) = *backing else {
            bio.complete(BioStatus::NotSupported);
            return Err(BioEnqueueError::Refused);
        };

        let mut current_sid: u64 = 0;
        for segment in bio.segments() {
            let start_offset = (sid_range.start + current_sid).to_raw() as usize * SECTOR_SIZE;
            let byte_len = segment.nbytes();
            let slice = segment.inner_dma_slice();

            let result = match type_ {
                BioType::Read => {
                    let mut buf = alloc::vec![0u8; byte_len];
                    let mut writer = VmWriter::from(&mut buf[..]).to_fallible();
                    backing_file
                        .read_at(start_offset, &mut writer)
                        .map_err(|_| BioEnqueueError::Refused)?;
                    slice
                        .write_bytes(0, &buf)
                        .map_err(|_| BioEnqueueError::Refused)
                }
                BioType::Write => {
                    let mut buf = alloc::vec![0u8; byte_len];
                    slice
                        .read_bytes(0, &mut buf)
                        .map_err(|_| BioEnqueueError::Refused)?;
                    let mut reader = VmReader::from(&buf[..]).to_fallible();
                    let _ = backing_file
                        .write_at(start_offset, &mut reader)
                        .map_err(|_| BioEnqueueError::Refused)?;
                    Ok(())
                }
                BioType::Flush => Ok(()),
            };

            if result.is_err() {
                bio.complete(BioStatus::IoError);
                return Err(BioEnqueueError::Refused);
            }

            current_sid += segment.nsectors().to_raw();
        }

        bio.complete(BioStatus::Complete);
        Ok(())
    }

    fn metadata(&self) -> BlockDeviceMeta {
        let size = self.size_bytes();
        BlockDeviceMeta {
            max_nr_segments_per_bio: 1,
            nr_sectors: if size > 0 { size / SECTOR_SIZE } else { 0 },
        }
    }

    fn name(&self) -> &str {
        &self.name
    }

    fn id(&self) -> DeviceId {
        self.id
    }
}

// ---------------------------------------------------------------------------
// /dev/loopN — Device + PerOpenFileOps
// ---------------------------------------------------------------------------

pub(crate) struct LoopFile(Arc<dyn BlockDevice>);

impl LoopFile {
    pub(crate) fn new(device: Arc<dyn BlockDevice>) -> Self {
        Self(device)
    }
}

impl Device for LoopFile {
    fn type_(&self) -> DeviceType {
        DeviceType::Block
    }

    fn id(&self) -> DeviceId {
        self.0.id()
    }

    fn devtmpfs_meta(&self) -> Option<DevtmpfsInodeMeta<'_>> {
        Some(DevtmpfsInodeMeta::new(self.0.name()))
    }

    fn open(&self) -> Result<Box<dyn PerOpenFileOps>> {
        Ok(Box::new(OpenLoopDevice {
            device: self.0.clone(),
        }))
    }
}

mod ioctl_defs {
    use crate::util::ioctl::{NoData, OutData, ioc};

    pub(super) type BlkGetSize64 = ioc!(BLKGETSIZE64, 0x12, 114, OutData<u64>);
    pub(super) type BlkGetSectorSize = ioc!(BLKSSZGET, 0x12, 104, NoData);
}

/// Per-open-file handle for a loop device.
struct OpenLoopDevice {
    device: Arc<dyn BlockDevice>,
}

impl OpenLoopDevice {
    fn loop_dev(&self) -> &LoopDevice {
        self.device
            .downcast_ref::<LoopDevice>()
            .expect("OpenLoopDevice must wrap a LoopDevice")
    }
}

impl FileOps for OpenLoopDevice {
    fn read_at(
        &self,
        offset: usize,
        writer: &mut VmWriter,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        let total = writer.avail();
        if total == 0 {
            return Ok(0);
        }
        let device_size = self.loop_dev().size_bytes();
        if offset >= device_size {
            return Ok(0);
        }
        let read_len = total.min(device_size - offset);
        {
            let mut limited_writer = writer.clone_exclusive();
            limited_writer.limit(read_len);
            let bd: &dyn BlockDevice = self.device.as_ref();
            VmIo::read(bd, offset, &mut limited_writer)?;
        }
        writer.skip(read_len);
        Ok(read_len)
    }

    fn write_at(
        &self,
        offset: usize,
        reader: &mut VmReader,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        if self.loop_dev().is_read_only() {
            return_errno_with_message!(Errno::EROFS, "the loop device is read-only");
        }

        let total = reader.remain();
        if total == 0 {
            return Ok(0);
        }
        let device_size = self.loop_dev().size_bytes();
        if offset >= device_size {
            return_errno_with_message!(
                Errno::ENOSPC,
                "the write offset is beyond the block device"
            );
        }
        let write_len = total.min(device_size - offset);
        {
            let mut limited_reader = reader.clone();
            limited_reader.limit(write_len);
            let bd: &dyn BlockDevice = self.device.as_ref();
            VmIo::write(bd, offset, &mut limited_reader)?;
        }
        reader.skip(write_len);
        Ok(write_len)
    }
}

impl Pollable for OpenLoopDevice {
    fn poll(&self, mask: IoEvents, _: Option<&mut PollHandle>) -> IoEvents {
        let events = IoEvents::IN | IoEvents::OUT;
        events & mask
    }
}

/// Computes the absolute path of a backing file for the sysfs
/// `loop/backing_file` attribute.
fn backing_file_path(file: &Arc<dyn FileLike>) -> Option<String> {
    let inode_handle = file.downcast_ref::<InodeHandle>()?;
    let task = ostd::task::Task::current()?;
    let thread_local = AsThreadLocal::as_thread_local(&task)?;
    let fs_info = thread_local.borrow_fs();
    let resolver = fs_info.resolver().read();
    Some(resolver.make_abs_path(inode_handle.path()).into_string())
}

/// Reads a `u32` field from an ioctl struct buffer.
fn read_u32(buf: &[u8], offset: usize) -> u32 {
    u32::from_le_bytes(buf[offset..offset + 4].try_into().unwrap())
}

/// Reads a `u64` field from an ioctl struct buffer.
fn read_u64(buf: &[u8], offset: usize) -> u64 {
    u64::from_le_bytes(buf[offset..offset + 8].try_into().unwrap())
}

/// Writes a `u32` field into an ioctl struct buffer.
fn write_u32(buf: &mut [u8], offset: usize, value: u32) {
    buf[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
}

/// Writes a `u64` field into an ioctl struct buffer.
fn write_u64(buf: &mut [u8], offset: usize, value: u64) {
    buf[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
}

/// Checks that a loop block size is valid (a power of two within
/// `[512, MAX_LOOP_BLOCK_SIZE]`), matching Linux's `loop_validate_block_size`.
fn is_valid_block_size(block_size: usize) -> bool {
    (512..=MAX_LOOP_BLOCK_SIZE).contains(&block_size) && block_size.is_power_of_two()
}

impl PerOpenFileOps for OpenLoopDevice {
    fn check_seekable(&self) -> Result<()> {
        Ok(())
    }

    fn is_offset_aware(&self) -> bool {
        true
    }

    fn seek_end(&self) -> Result<Option<usize>> {
        Ok(Some(self.loop_dev().size_bytes()))
    }

    fn ioctl(&self, _path: &Path, raw_ioctl: RawIoctl) -> Result<i32> {
        use ioctl_defs::*;

        dispatch_ioctl!(match raw_ioctl {
            _cmd @ BlkGetSectorSize => {
                let sector_size = SECTOR_SIZE as i32;
                current_userspace!().write_val(raw_ioctl.arg(), &sector_size)?;
                Ok(0)
            }
            cmd @ BlkGetSize64 => {
                let size = self.loop_dev().size_bytes() as u64;
                cmd.write(&size)?;
                Ok(0)
            }
            _ => return_errno_with_message!(
                Errno::ENOTTY,
                "the ioctl command is not supported by loop devices"
            ),
        })
    }

    fn ioctl_with_table(
        &self,
        _path: &Path,
        raw_ioctl: RawIoctl,
        file_table: &mut FileTableRefMut,
    ) -> Option<Result<i32>> {
        use crate::fs::file::file_table::FileDesc;

        let cmd = raw_ioctl.cmd();
        if ((cmd >> 8) & 0xFF) as u8 != LOOP_MAGIC {
            return None;
        }

        // Looks up a backing file descriptor in the file table.
        macro_rules! lookup_backing_file {
            ($fd:expr) => {
                match FileDesc::try_from($fd).ok().and_then(|fd| {
                    file_table
                        .read_with(|inner| inner.get_file(fd).cloned())
                        .ok()
                }) {
                    Some(file) => file,
                    None => return Some(Err(Error::new(Errno::EBADF))),
                }
            };
        }

        match cmd {
            LOOP_SET_FD => {
                let backing_file = lookup_backing_file!(raw_ioctl.arg() as i32);
                let path = backing_file_path(&backing_file);
                match self.loop_dev().set_backing_file(backing_file, path) {
                    Ok(()) => Some(Ok(0)),
                    Err(e) => Some(Err(e)),
                }
            }
            LOOP_CLR_FD => {
                // Like Linux, clearing an unbound loop device fails with ENXIO.
                if !self.loop_dev().has_backing_file() {
                    return Some(Err(Error::new(Errno::ENXIO)));
                }
                self.loop_dev().clear_backing_file();
                Some(Ok(0))
            }
            LOOP_SET_STATUS => {
                let mut buf = [0u8; loop_info::SIZE];
                match current_userspace!().read_bytes(raw_ioctl.arg(), &mut buf) {
                    Ok(()) => {
                        let flags = read_u32(&buf, loop_info::LO_FLAGS);
                        let name = &buf[loop_info::LO_NAME..loop_info::LO_NAME + LO_NAME_SIZE];
                        // The legacy LOOP_SET_STATUS carries no `lo_sizelimit`
                        // field; Linux resets the limit to 0 (unlimited).
                        match self.loop_dev().set_status(flags, Some(0), Some(name)) {
                            Ok(()) => Some(Ok(0)),
                            Err(e) => Some(Err(e)),
                        }
                    }
                    Err(e) => Some(Err(e.into())),
                }
            }
            LOOP_GET_STATUS => {
                if !self.loop_dev().has_backing_file() {
                    return Some(Err(Error::new(Errno::ENXIO)));
                }
                let mut buf = [0u8; loop_info::SIZE];
                write_u32(&mut buf, loop_info::LO_FLAGS, self.loop_dev().lo_flags());
                let lo_name = self.loop_dev().lo_name();
                buf[loop_info::LO_NAME..loop_info::LO_NAME + LO_NAME_SIZE]
                    .copy_from_slice(&lo_name);
                match current_userspace!().write_bytes(raw_ioctl.arg(), &buf) {
                    Ok(()) => Some(Ok(0)),
                    Err(e) => Some(Err(e.into())),
                }
            }
            LOOP_SET_STATUS64 => {
                let mut buf = [0u8; loop_info64::SIZE];
                match current_userspace!().read_bytes(raw_ioctl.arg(), &mut buf) {
                    Ok(()) => {
                        let flags = read_u32(&buf, loop_info64::LO_FLAGS);
                        let sizelimit = read_u64(&buf, loop_info64::LO_SIZELIMIT);
                        let name = &buf
                            [loop_info64::LO_FILE_NAME..loop_info64::LO_FILE_NAME + LO_NAME_SIZE];
                        match self
                            .loop_dev()
                            .set_status(flags, Some(sizelimit), Some(name))
                        {
                            Ok(()) => Some(Ok(0)),
                            Err(e) => Some(Err(e)),
                        }
                    }
                    Err(e) => Some(Err(e.into())),
                }
            }
            LOOP_GET_STATUS64 => {
                if !self.loop_dev().has_backing_file() {
                    return Some(Err(Error::new(Errno::ENXIO)));
                }
                let mut buf = [0u8; loop_info64::SIZE];
                write_u32(&mut buf, loop_info64::LO_FLAGS, self.loop_dev().lo_flags());
                write_u64(
                    &mut buf,
                    loop_info64::LO_SIZELIMIT,
                    self.loop_dev().sizelimit(),
                );
                let lo_name = self.loop_dev().lo_name();
                buf[loop_info64::LO_FILE_NAME..loop_info64::LO_FILE_NAME + LO_NAME_SIZE]
                    .copy_from_slice(&lo_name);
                match current_userspace!().write_bytes(raw_ioctl.arg(), &buf) {
                    Ok(()) => Some(Ok(0)),
                    Err(e) => Some(Err(e.into())),
                }
            }
            LOOP_CHANGE_FD => {
                let backing_file = lookup_backing_file!(raw_ioctl.arg() as i32);
                let path = backing_file_path(&backing_file);
                match self.loop_dev().change_backing_file(backing_file, path) {
                    Ok(()) => Some(Ok(0)),
                    Err(e) => Some(Err(e)),
                }
            }
            LOOP_SET_CAPACITY => match self.loop_dev().refresh_capacity() {
                Ok(()) => Some(Ok(0)),
                Err(e) => Some(Err(e)),
            },
            LOOP_SET_BLOCK_SIZE => {
                let block_size = raw_ioctl.arg();
                if !is_valid_block_size(block_size) {
                    return Some(Err(Error::new(Errno::EINVAL)));
                }
                Some(Ok(0))
            }
            LOOP_CONFIGURE => {
                let mut buf = [0u8; loop_config::SIZE];
                if let Err(e) = current_userspace!().read_bytes(raw_ioctl.arg(), &mut buf) {
                    return Some(Err(e.into()));
                }

                let block_size = read_u32(&buf, loop_config::BLOCK_SIZE) as usize;
                if block_size != 0 && !is_valid_block_size(block_size) {
                    return Some(Err(Error::new(Errno::EINVAL)));
                }

                let backing_file = lookup_backing_file!(read_u32(&buf, loop_config::FD) as i32);
                let path = backing_file_path(&backing_file);
                let flags = read_u32(&buf, loop_config::INFO + loop_info64::LO_FLAGS);
                let sizelimit = read_u64(&buf, loop_config::INFO + loop_info64::LO_SIZELIMIT);
                let name = &buf[loop_config::INFO + loop_info64::LO_FILE_NAME
                    ..loop_config::INFO + loop_info64::LO_FILE_NAME + LO_NAME_SIZE];
                match self
                    .loop_dev()
                    .configure(backing_file, path, flags, sizelimit, Some(name))
                {
                    Ok(()) => Some(Ok(0)),
                    Err(e) => Some(Err(e)),
                }
            }
            _ => None,
        }
    }

    fn settable_status_flags(&self) -> SettableStatusFlags {
        SettableStatusFlags::minimal().with_o_direct()
    }
}

// ---------------------------------------------------------------------------
// /sys/block/loopN — sysfs nodes
// ---------------------------------------------------------------------------

/// The `/sys/block` branch node.
#[derive(Debug)]
struct BlockSysNode {
    fields: AttrLessBranchNodeFields<dyn SysObj, Self>,
}

#[inherit_methods(from = "self.fields")]
impl BlockSysNode {
    fn new() -> Arc<Self> {
        Arc::new_cyclic(|weak_self| Self {
            fields: AttrLessBranchNodeFields::new(SysStr::from("block"), weak_self.clone()),
        })
    }

    fn add_child(&self, new_child: Arc<dyn SysObj>) -> SysTreeResult<()>;
}

inherit_sys_branch_node!(BlockSysNode, fields, {
    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

/// A `/sys/block/loopN` branch node exposing the generic block attributes
/// (`ro`, `size`) and the `loop/` subdirectory.
#[derive(Debug)]
struct LoopSysNode {
    fields: BranchNodeFields<dyn SysObj, Self>,
    device: Arc<LoopDevice>,
}

#[inherit_methods(from = "self.fields")]
impl LoopSysNode {
    fn new(device: Arc<LoopDevice>) -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        builder.add(SysStr::from("ro"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        builder.add(SysStr::from("size"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        let attrs = builder
            .build()
            .expect("failed to build the loop device attribute set");

        let sys_node = Arc::new_cyclic(|weak_self| Self {
            fields: BranchNodeFields::new(
                SysStr::from(device.name.clone()),
                attrs,
                weak_self.clone(),
            ),
            device: device.clone(),
        });
        sys_node
            .add_child(LoopInfoSysNode::new(device))
            .expect("failed to register the loop info sysfs node");
        sys_node
    }

    fn add_child(&self, new_child: Arc<dyn SysObj>) -> SysTreeResult<()>;
}

inherit_sys_branch_node!(LoopSysNode, fields, {
    fn read_attr_at(
        &self,
        name: &str,
        offset: usize,
        writer: &mut VmWriter,
    ) -> SysTreeResult<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        match name {
            "ro" => writeln!(printer, "{}", self.device.is_read_only() as u32)?,
            "size" => writeln!(printer, "{}", self.device.size_bytes() / SECTOR_SIZE)?,
            _ => return Err(SysTreeError::AttributeError),
        }
        Ok(printer.bytes_written())
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

/// The `/sys/block/loopN/loop` node exposing the loop-specific attributes
/// (`backing_file`, `partscan`, `autoclear`, `sizelimit`).
#[derive(Debug)]
struct LoopInfoSysNode {
    fields: NormalNodeFields<Self>,
    device: Arc<LoopDevice>,
}

impl LoopInfoSysNode {
    fn new(device: Arc<LoopDevice>) -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        for name in ["partscan", "autoclear", "backing_file", "sizelimit"] {
            builder.add(SysStr::from(name), SysPerms::DEFAULT_RO_ATTR_PERMS);
        }
        let attrs = builder
            .build()
            .expect("failed to build the loop info attribute set");

        Arc::new_cyclic(|weak_self| Self {
            fields: NormalNodeFields::new(SysStr::from("loop"), attrs, weak_self.clone()),
            device,
        })
    }
}

inherit_sys_leaf_node!(LoopInfoSysNode, fields, {
    fn read_attr_at(
        &self,
        name: &str,
        offset: usize,
        writer: &mut VmWriter,
    ) -> SysTreeResult<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        match name {
            "partscan" => writeln!(
                printer,
                "{}",
                (self.device.lo_flags() & LO_FLAGS_PARTSCAN != 0) as u32
            )?,
            "autoclear" => writeln!(
                printer,
                "{}",
                (self.device.lo_flags() & LO_FLAGS_AUTOCLEAR != 0) as u32
            )?,
            "sizelimit" => writeln!(printer, "{}", self.device.sizelimit())?,
            "backing_file" => {
                if let Some(path) = self.device.backing_path() {
                    writeln!(printer, "{}", path)?;
                } else {
                    writeln!(printer)?;
                }
            }
            _ => return Err(SysTreeError::AttributeError),
        }
        Ok(printer.bytes_written())
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

// ---------------------------------------------------------------------------
// /dev/loop-control — character device
// ---------------------------------------------------------------------------

struct LoopControlDevice {
    id: DeviceId,
}

impl LoopControlDevice {
    fn new(id: DeviceId) -> Arc<Self> {
        Arc::new(Self { id })
    }
}

impl Device for LoopControlDevice {
    fn type_(&self) -> DeviceType {
        DeviceType::Char
    }

    fn id(&self) -> DeviceId {
        self.id
    }

    fn devtmpfs_meta(&self) -> Option<DevtmpfsInodeMeta<'_>> {
        Some(DevtmpfsInodeMeta::new("loop-control"))
    }

    fn open(&self) -> Result<Box<dyn PerOpenFileOps>> {
        Ok(Box::new(LoopControlFile))
    }
}

struct LoopControlFile;

impl FileOps for LoopControlFile {
    fn read_at(
        &self,
        _offset: usize,
        _writer: &mut VmWriter,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EINVAL, "loop-control does not support read");
    }

    fn write_at(
        &self,
        _offset: usize,
        _reader: &mut VmReader,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EINVAL, "loop-control does not support write");
    }
}

impl Pollable for LoopControlFile {
    fn poll(&self, _mask: IoEvents, _: Option<&mut PollHandle>) -> IoEvents {
        IoEvents::empty()
    }
}

impl PerOpenFileOps for LoopControlFile {
    fn check_seekable(&self) -> Result<()> {
        return_errno_with_message!(Errno::ESPIPE, "loop-control is not seekable");
    }

    fn is_offset_aware(&self) -> bool {
        false
    }

    fn ioctl(&self, _path: &Path, raw_ioctl: RawIoctl) -> Result<i32> {
        let cmd = raw_ioctl.cmd();
        if ((cmd >> 8) & 0xFF) as u8 != LOOP_MAGIC {
            return_errno_with_message!(Errno::ENOTTY, "unknown ioctl");
        }

        match cmd {
            LOOP_CTL_GET_FREE => {
                let devices = LOOP_DEVICES.lock();
                for (i, device) in devices.iter().enumerate() {
                    if !device.has_backing_file() {
                        return Ok(i as i32);
                    }
                }
                return_errno_with_message!(Errno::ENFILE, "no free loop devices");
            }
            _ => return_errno_with_message!(Errno::ENOTTY, "unknown ioctl"),
        }
    }
}

// ---------------------------------------------------------------------------
// Static registry and bootstrap
// ---------------------------------------------------------------------------

static LOOP_MAJOR: Mutex<Option<aster_block::MajorIdOwner>> = Mutex::new(None);
static LOOP_DEVICES: Mutex<Vec<Arc<LoopDevice>>> = Mutex::new(Vec::new());
static LOOP_CONTROL_MAJOR: Mutex<Option<CharMajorIdOwner>> = Mutex::new(None);
static BLOCK_SYS_NODE: Once<Arc<BlockSysNode>> = Once::new();

pub(super) fn init_in_first_kthread() {
    let major = aster_block::allocate_major().expect("failed to allocate loop block major");
    *LOOP_MAJOR.lock() = Some(major);

    // Create the `/sys/block` branch and one `/sys/block/loopN` node per loop
    // device. The sysfs singleton is initialized before device init.
    let block_sys_node = BLOCK_SYS_NODE.call_once(BlockSysNode::new);
    crate::fs::sysfs::systree_singleton()
        .root()
        .add_child(block_sys_node.clone() as Arc<dyn SysObj>)
        .expect("failed to register the /sys/block sysfs node");

    let mut devices = LOOP_DEVICES.lock();
    for i in 0..NUM_LOOP_DEVICES {
        let minor = MinorId::new(i);
        let id = DeviceId::new(LOOP_MAJOR.lock().as_ref().unwrap().get(), minor);
        let name = alloc::format!("loop{}", i);
        let device = LoopDevice::new(id, &name);
        aster_block::register(device.clone()).expect("failed to register loop device");
        block_sys_node
            .add_child(LoopSysNode::new(device.clone()) as Arc<dyn SysObj>)
            .expect("failed to register the loop device sysfs node");
        devices.push(device);
    }
}

pub(super) fn init_in_first_process() -> Result<()> {
    // /dev/loopN nodes are created automatically by the block registry
    // (block::init_in_first_process) which iterates all registered block
    // devices. The active char registry likewise creates /dev/loop-control
    // when the control device is registered below.

    let ctl_major = char::allocate_major().expect("failed to allocate loop-control major");
    *LOOP_CONTROL_MAJOR.lock() = Some(ctl_major);
    let ctl_id = DeviceId::new(
        LOOP_CONTROL_MAJOR.lock().as_ref().unwrap().get(),
        MinorId::new(237),
    );
    let ctl_device = LoopControlDevice::new(ctl_id);
    char::register(ctl_device).expect("failed to register loop-control");

    Ok(())
}
