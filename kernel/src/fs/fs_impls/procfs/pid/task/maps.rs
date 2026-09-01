// SPDX-License-Identifier: MPL-2.0

use super::TidDirOps;
use crate::{
    events::IoEvents,
    fs::{
        file::{AccessMode, PerOpenFileOps, StatusFlags, mkmod},
        procfs::template::{ProcFile, ProcFileOpsByHandle},
        vfs::inode::{FileOps, Inode},
    },
    prelude::*,
    process::{
        VmarSnapshot,
        posix_thread::{AsPosixThread, alien_access::AlienAccessMode},
        signal::{PollHandle, Pollable},
    },
    thread::Thread,
    vm::vmar::{VMAR_CAP_ADDR, VMAR_LOWEST_ADDR},
};

/// Represents the inode at `/proc/[pid]/task/[tid]/maps` (and also `/proc/[pid]/maps`).
pub struct MapsFileOps(TidDirOps);

impl MapsFileOps {
    pub fn new_inode(dir: &TidDirOps, parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference: <https://elixir.bootlin.com/linux/v6.16.5/source/fs/proc/base.c#L3343>
        ProcFile::new(Self(dir.clone()), parent, mkmod!(a+r))
    }
}

impl ProcFileOpsByHandle for MapsFileOps {
    fn owner_thread(&self) -> Option<Arc<Thread>> {
        self.0.thread()
    }

    fn open(
        &self,
        _access_mode: AccessMode,
        _status_flags: StatusFlags,
    ) -> Result<Box<dyn PerOpenFileOps>> {
        let Some(process) = self.0.process() else {
            return_errno_with_message!(Errno::ESRCH, "the process does not exist");
        };
        // Hold the process VMAR lock while checking access permissions and
        // taking the VMAR identity snapshot to prevent race conditions.
        let vmar_guard = process.lock_vmar();

        process
            .main_thread()
            .as_posix_thread()
            .unwrap()
            .check_alien_access_from(
                current_thread!().as_posix_thread().unwrap(),
                AlienAccessMode::READ_WITH_FS_CREDS,
            )
            .map_err(|_| Error::with_message(Errno::EACCES, "alien access is denied"))?;

        let vmar = vmar_guard.snapshot();
        Ok(Box::new(MapsFileHandle(
            self.0.clone(),
            vmar,
            Mutex::new(None),
        )))
    }
}

/// A file handle opened from `/proc/[pid]/task/[tid]/maps` (and also `/proc/[pid]/maps`).
struct MapsFileHandle(TidDirOps, VmarSnapshot, Mutex<Option<String>>);

/// Returns the part of a rendered maps snapshot that fits in one read.
///
/// Keeping this arithmetic separate makes it difficult for the cached path to
/// accidentally copy past EOF when userspace seeks or supplies a zero-sized
/// buffer.
fn snapshot_slice(snapshot: &[u8], offset: usize, max_len: usize) -> &[u8] {
    if offset >= snapshot.len() {
        return &[];
    }
    let copy_len = (snapshot.len() - offset).min(max_len);
    &snapshot[offset..offset + copy_len]
}

impl Pollable for MapsFileHandle {
    fn poll(&self, mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        let events = IoEvents::IN | IoEvents::OUT;
        events & mask
    }
}

impl FileOps for MapsFileHandle {
    fn read_at(
        &self,
        offset: usize,
        writer: &mut VmWriter,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        let Some(process) = self.0.process() else {
            return_errno_with_message!(Errno::ESRCH, "the process does not exist");
        };
        let vmar_guard = process.lock_vmar();
        if !vmar_guard.is_same_as(&self.1) {
            // The process has executed a new program.
            return Ok(0);
        }
        let Some(vmar) = vmar_guard.as_ref() else {
            // The process has exited.
            return Ok(0);
        };

        // Firefox reads `/proc/self/maps` in small chunks.  Rendering from the
        // beginning for every chunk makes each read walk every mapping and
        // resolve every path again, turning a normally cheap proc file into an
        // O(number_of_chunks * number_of_mappings) operation.  Cache one
        // rendered snapshot per open handle and only copy the requested slice
        // on subsequent reads.
        let mut cached = self.2.lock();
        if cached.is_none() {
            let current = current_thread!();
            let fs_ref = current.as_posix_thread().unwrap().read_fs();
            let path_resolver = fs_ref.resolver().read();

            // To maintain a consistent lock order and avoid race conditions, we must lock the
            // heap before querying the VMAR.
            let heap_guard = vmar.process_vm().heap().lock();
            let guard = vmar.query(VMAR_LOWEST_ADDR..VMAR_CAP_ADDR);
            let mut snapshot = String::new();
            for vm_mapping in guard.iter() {
                vm_mapping.print_to_maps(&mut snapshot, vmar, &heap_guard, &path_resolver)?;
            }
            *cached = Some(snapshot);
        }

        let snapshot = cached.as_ref().unwrap().as_bytes();
        let mut reader = VmReader::from(snapshot_slice(snapshot, offset, writer.avail()));
        Ok(writer.write_fallible(&mut reader).map_err(|(err, _)| err)?)
    }

    fn write_at(
        &self,
        _offset: usize,
        _reader: &mut VmReader,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EPERM, "`/proc/[pid]/maps` is not writable");
    }
}

impl PerOpenFileOps for MapsFileHandle {
    fn check_seekable(&self) -> Result<()> {
        Ok(())
    }

    fn is_offset_aware(&self) -> bool {
        true
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::snapshot_slice;

    #[ktest]
    fn maps_snapshot_slice_respects_offset_and_capacity() {
        let snapshot = b"abcdef";
        assert_eq!(snapshot_slice(snapshot, 0, 3), b"abc");
        assert_eq!(snapshot_slice(snapshot, 2, 99), b"cdef");
        assert_eq!(snapshot_slice(snapshot, 6, 3), b"");
        assert_eq!(snapshot_slice(snapshot, 7, 0), b"");
        assert_eq!(snapshot_slice(snapshot, 1, 0), b"");
    }
}
