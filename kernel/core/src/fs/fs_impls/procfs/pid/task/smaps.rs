// SPDX-License-Identifier: MPL-2.0

//! `/proc/[pid]/task/[tid]/smaps` (and `/proc/[pid]/smaps`): the memory map
//! with per-mapping statistics.
//!
//! `pmap`, `smem` and `/proc` scrapers read this instead of `/proc/pid/maps`
//! because the extra `Size`/`Rss`/`Pss` lines are what they actually report.
//! The header line of each entry is the `maps` line, so both files agree on
//! addresses, permissions and paths.
//!
//! Reference: <https://man7.org/linux/man-pages/man5/proc_pid_smaps.5.html>

use core::fmt::Write;

use ostd::mm::PAGE_SIZE;

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
    vm::{
        perms::VmPerms,
        vmar::{VMAR_CAP_ADDR, VMAR_LOWEST_ADDR, VmMapping},
    },
};

/// Represents the inode at `/proc/[pid]/task/[tid]/smaps` (and `/proc/[pid]/smaps`).
pub struct SmapsFileOps(TidDirOps);

impl SmapsFileOps {
    pub fn new_inode(dir: &TidDirOps, parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self(dir.clone()), parent, mkmod!(a+r))
    }
}

impl ProcFileOpsByHandle for SmapsFileOps {
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
        Ok(Box::new(SmapsFileHandle(
            self.0.clone(),
            vmar,
            Mutex::new(None),
        )))
    }
}

/// A file handle opened from `/proc/[pid]/task/[tid]/smaps`.
struct SmapsFileHandle(TidDirOps, VmarSnapshot, Mutex<Option<String>>);

impl Pollable for SmapsFileHandle {
    fn poll(&self, mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        IoEvents::IN | IoEvents::OUT & mask
    }
}

impl FileOps for SmapsFileHandle {
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

        // Render once per open handle: reading a process map in small chunks
        // must not re-walk every mapping for every chunk.
        let mut cached = self.2.lock();
        if cached.is_none() {
            let current = current_thread!();
            let fs_ref = current.as_posix_thread().unwrap().read_fs();
            let path_resolver = fs_ref.resolver().read();

            // Keep the same lock order as `/proc/pid/maps`: heap before VMAR
            // query.
            let heap_guard = vmar.process_vm().heap().lock();
            let guard = vmar.query(VMAR_LOWEST_ADDR..VMAR_CAP_ADDR);
            let mut snapshot = String::new();
            for vm_mapping in guard.iter() {
                vm_mapping.print_to_maps(&mut snapshot, vmar, &heap_guard, &path_resolver)?;
                print_statistics(&mut snapshot, vm_mapping)?;
            }
            *cached = Some(snapshot);
        }

        let snapshot = cached.as_ref().unwrap().as_bytes();
        let Some(remaining) = snapshot.get(offset..) else {
            return Ok(0);
        };
        let len = remaining.len().min(writer.avail());
        let mut reader = VmReader::from(&remaining[..len]);
        Ok(writer.write_fallible(&mut reader).map_err(|(err, _)| err)?)
    }

    fn write_at(
        &self,
        _offset: usize,
        _reader: &mut VmReader,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EPERM, "`/proc/[pid]/smaps` is not writable");
    }
}

impl PerOpenFileOps for SmapsFileHandle {
    fn check_seekable(&self) -> Result<()> {
        Ok(())
    }

    fn is_offset_aware(&self) -> bool {
        true
    }
}

/// Appends the statistics block that follows a mapping's `maps` line.
///
/// Fields the kernel does not track are printed as zero with their Linux
/// names, because readers look them up by name and treat a missing name as an
/// error rather than as "unknown".
fn print_statistics(out: &mut String, mapping: &crate::vm::vmar::VmMapping) -> Result<()> {
    let page_kb = PAGE_SIZE / 1024;
    let size_kb = mapping.map_size() / 1024;
    let rss_kb = mapping.resident_pages() * page_kb;

    // Every resident page is private to this mapping: shared, copy-on-write
    // accounting is not modeled, so `Pss` is the same as `Rss` and the shared
    // counters stay at zero.
    let writable = mapping.perms().contains(VmPerms::WRITE);

    writeln!(out, "Size:           {:>8} kB", size_kb).map_err(write_error)?;
    writeln!(out, "KernelPageSize: {:>8} kB", page_kb).map_err(write_error)?;
    writeln!(out, "MMUPageSize:    {:>8} kB", page_kb).map_err(write_error)?;
    writeln!(out, "Rss:            {:>8} kB", rss_kb).map_err(write_error)?;
    writeln!(out, "Pss:            {:>8} kB", rss_kb).map_err(write_error)?;
    writeln!(out, "Shared_Clean:   {:>8} kB", 0).map_err(write_error)?;
    writeln!(out, "Shared_Dirty:   {:>8} kB", 0).map_err(write_error)?;
    writeln!(
        out,
        "Private_Clean:  {:>8} kB",
        if writable { 0 } else { rss_kb }
    )
    .map_err(write_error)?;
    writeln!(
        out,
        "Private_Dirty:  {:>8} kB",
        if writable { rss_kb } else { 0 }
    )
    .map_err(write_error)?;
    writeln!(out, "Referenced:     {:>8} kB", rss_kb).map_err(write_error)?;
    writeln!(out, "Anonymous:      {:>8} kB", 0).map_err(write_error)?;
    writeln!(out, "LazyFree:       {:>8} kB", 0).map_err(write_error)?;
    writeln!(out, "AnonHugePages:  {:>8} kB", 0).map_err(write_error)?;
    writeln!(out, "ShmemPmdMapped: {:>8} kB", 0).map_err(write_error)?;
    writeln!(out, "FilePmdMapped:  {:>8} kB", 0).map_err(write_error)?;
    writeln!(out, "Shared_Hugetlb: {:>8} kB", 0).map_err(write_error)?;
    writeln!(out, "Private_Hugetlb:{:>8} kB", 0).map_err(write_error)?;
    writeln!(out, "Swap:           {:>8} kB", 0).map_err(write_error)?;
    writeln!(out, "SwapPss:        {:>8} kB", 0).map_err(write_error)?;
    writeln!(out, "Locked:         {:>8} kB", 0).map_err(write_error)?;
    writeln!(out, "THPeligible:    {:>8}", 0).map_err(write_error)?;
    writeln!(out, "VmFlags: {}", vm_flags(mapping.perms())).map_err(write_error)?;
    Ok(())
}

fn write_error(_: core::fmt::Error) -> Error {
    Error::with_message(Errno::ENOMEM, "failed to render `/proc/[pid]/smaps`")
}

/// The `VmFlags` letters Linux reports for the permissions we model.
fn vm_flags(perms: VmPerms) -> &'static str {
    let read = perms.contains(VmPerms::READ);
    let write = perms.contains(VmPerms::WRITE);
    let exec = perms.contains(VmPerms::EXEC);
    match (read, write, exec) {
        (true, true, true) => "rd wr ex mr mw me",
        (true, true, false) => "rd wr mr mw me",
        (true, false, true) => "rd ex mr me",
        (true, false, false) => "rd mr me",
        (false, true, true) => "wr ex mw me",
        (false, true, false) => "wr mw me",
        (false, false, true) => "ex me",
        (false, false, false) => "",
    }
}
