// SPDX-License-Identifier: MPL-2.0

//! Loop device subsystem.
//!
//! Provides `/dev/loopN` block devices backed by regular files, and
//! `/dev/loop-control` for dynamic allocation. This is the minimal
//! implementation needed by LTP's `tst_device.c`.

use core::sync::atomic::{AtomicUsize, Ordering};

use aster_block::{
    BlockDevice, BlockDeviceMeta, SECTOR_SIZE,
    bio::{BioEnqueueError, BioStatus, BioType, SubmittedBio},
};
use device_id::{DeviceId, MinorId};
use ostd::mm::VmIo;

use crate::{
    context::current_userspace,
    device::{
        Device, DeviceType, DevtmpfsInodeMeta, add_node,
        registry::char::{self, MajorIdOwner as CharMajorIdOwner},
    },
    events::IoEvents,
    fs::{
        file::{
            FileLike, PerOpenFileOps, SeekFrom, SettableStatusFlags, StatusFlags,
            file_table::WithFileTable,
        },
        vfs::{
            inode::FileOps,
            path::{Path, PathResolver},
        },
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
const LOOP_MAGIC: u8 = 0x4C;

/// ioctl command numbers (from linux/loop.h).
const LOOP_SET_FD: u32 = 0x4C00;
const LOOP_CLR_FD: u32 = 0x4C01;
const LOOP_SET_STATUS: u32 = 0x4C02;
const LOOP_GET_STATUS: u32 = 0x4C03;
const LOOP_CTL_GET_FREE: u32 = 0x4C82;

/// Size of the `lo_name` field in `struct loop_info`.
const LO_NAME_SIZE: usize = 64;

/// A loop block device backed by a regular file.
pub struct LoopDevice {
    id: DeviceId,
    name: String,
    backing_file: Mutex<Option<Arc<dyn FileLike>>>,
    device_size: AtomicUsize,
    lo_name: Mutex<[u8; LO_NAME_SIZE]>,
}

impl Debug for LoopDevice {
    fn fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result {
        f.debug_struct("LoopDevice")
            .field("id", &self.id)
            .field("name", &self.name)
            .field("device_size", &self.device_size)
            .finish()
    }
}

impl LoopDevice {
    fn new(id: DeviceId, name: &str) -> Arc<Self> {
        Arc::new(Self {
            id,
            name: name.into(),
            backing_file: Mutex::new(None),
            device_size: AtomicUsize::new(0),
            lo_name: Mutex::new([0u8; LO_NAME_SIZE]),
        })
    }

    fn size_bytes(&self) -> usize {
        self.device_size.load(Ordering::Relaxed)
    }

    fn set_backing_file(&self, file: Arc<dyn FileLike>) -> Result<()> {
        let size = file.seek(SeekFrom::End(0))?;
        *self.backing_file.lock() = Some(file);
        self.device_size.store(size, Ordering::Relaxed);
        Ok(())
    }

    fn clear_backing_file(&self) {
        *self.backing_file.lock() = None;
        self.device_size.store(0, Ordering::Relaxed);
    }

    fn has_backing_file(&self) -> bool {
        self.backing_file.lock().is_some()
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
            let start_offset =
                (sid_range.start + current_sid).to_raw() as usize * SECTOR_SIZE;
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

struct LoopFile(Arc<LoopDevice>);

impl LoopFile {
    fn new(device: Arc<LoopDevice>) -> Self {
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
    device: Arc<LoopDevice>,
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
        let device_size = self.device.size_bytes();
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
        let total = reader.remain();
        if total == 0 {
            return Ok(0);
        }
        let device_size = self.device.size_bytes();
        if offset >= device_size {
            return_errno_with_message!(Errno::ENOSPC, "the write offset is beyond the block device");
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

impl PerOpenFileOps for OpenLoopDevice {
    fn check_seekable(&self) -> Result<()> {
        Ok(())
    }

    fn is_offset_aware(&self) -> bool {
        true
    }

    fn seek_end(&self) -> Result<Option<usize>> {
        Ok(Some(self.device.size_bytes()))
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
                let size = self.device.size_bytes() as u64;
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
        let cmd = raw_ioctl.cmd();
        if ((cmd >> 8) & 0xFF) as u8 != LOOP_MAGIC {
            return None;
        }

        match cmd {
            LOOP_SET_FD => {
                use crate::fs::file::file_table::FileDesc;
                let backing_fd = raw_ioctl.arg() as i32;
                let fd = FileDesc::try_from(backing_fd).ok()?;
                let backing_file = file_table.read_with(|inner| {
                    inner.get_file(fd).map(|f| f.clone())
                });
                let backing_file = match backing_file {
                    Ok(f) => f,
                    Err(_) => return Some(Err(Error::new(Errno::EBADF))),
                };
                match self.device.set_backing_file(backing_file) {
                    Ok(()) => Some(Ok(0)),
                    Err(e) => Some(Err(e.into())),
                }
            }
            LOOP_CLR_FD => {
                self.device.clear_backing_file();
                Some(Ok(0))
            }
            LOOP_SET_STATUS => {
                let mut buf = [0u8; 132];
                match current_userspace!().read_bytes(raw_ioctl.arg(), &mut buf) {
                    Ok(()) => {
                        self.device.set_lo_name(&buf[28..92]);
                        Some(Ok(0))
                    }
                    Err(e) => Some(Err(e.into())),
                }
            }
            LOOP_GET_STATUS => {
                if !self.device.has_backing_file() {
                    return Some(Err(Error::new(Errno::ENXIO)));
                }
                let mut buf = [0u8; 132];
                let lo_name = self.device.lo_name();
                buf[28..92].copy_from_slice(&lo_name);
                match current_userspace!().write_bytes(raw_ioctl.arg(), &buf) {
                    Ok(()) => Some(Ok(0)),
                    Err(e) => Some(Err(e.into())),
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

pub(super) fn init_in_first_kthread() {
    let major = aster_block::allocate_major().expect("failed to allocate loop block major");
    *LOOP_MAJOR.lock() = Some(major);

    let mut devices = LOOP_DEVICES.lock();
    for i in 0..NUM_LOOP_DEVICES {
        let minor = MinorId::new(i);
        let id = DeviceId::new(LOOP_MAJOR.lock().as_ref().unwrap().get(), minor);
        let name = alloc::format!("loop{}", i);
        let device = LoopDevice::new(id, &name);
        aster_block::register(device.clone()).expect("failed to register loop device");
        devices.push(device);
    }
}

pub(super) fn init_in_first_process(path_resolver: &PathResolver) -> Result<()> {
    let devices = LOOP_DEVICES.lock();
    for device in devices.iter() {
        let file = Arc::new(LoopFile::new(device.clone()));
        if let Some(meta) = file.devtmpfs_meta() {
            let dev_id = file.id().as_encoded_u64();
            add_node(DeviceType::Block, dev_id, &meta, path_resolver)?;
        }
    }

    let ctl_major = char::allocate_major().expect("failed to allocate loop-control major");
    *LOOP_CONTROL_MAJOR.lock() = Some(ctl_major);
    let ctl_id = DeviceId::new(
        LOOP_CONTROL_MAJOR.lock().as_ref().unwrap().get(),
        MinorId::new(237),
    );
    let ctl_device = LoopControlDevice::new(ctl_id);
    char::register(ctl_device).expect("failed to register loop-control");
    let ctl_meta = DevtmpfsInodeMeta::new("loop-control");
    add_node(DeviceType::Char, ctl_id.as_encoded_u64(), &ctl_meta, path_resolver)?;

    Ok(())
}