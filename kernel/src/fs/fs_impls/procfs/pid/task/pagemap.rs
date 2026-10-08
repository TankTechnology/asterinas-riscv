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
    vm::vmar::VMAR_CAP_ADDR,
};

const PAGEMAP_ENTRY_SIZE: usize = size_of::<u64>();
const PAGEMAP_PRESENT: u64 = 1 << 63;

/// Represents `/proc/[pid]/pagemap` and `/proc/[pid]/task/[tid]/pagemap`.
pub struct PagemapFileOps(TidDirOps);

impl PagemapFileOps {
    pub fn new_inode(dir: &TidDirOps, parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference: <https://docs.kernel.org/admin-guide/mm/pagemap.html>.
        ProcFile::new(Self(dir.clone()), parent, mkmod!(u+r))
    }
}

impl ProcFileOpsByHandle for PagemapFileOps {
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

        Ok(Box::new(PagemapFileHandle(
            self.0.clone(),
            vmar_guard.snapshot(),
        )))
    }
}

struct PagemapFileHandle(TidDirOps, VmarSnapshot);

impl Pollable for PagemapFileHandle {
    fn poll(&self, mask: IoEvents, _poller: Option<&mut PollHandle>) -> IoEvents {
        IoEvents::IN & mask
    }
}

impl FileOps for PagemapFileHandle {
    fn read_at(
        &self,
        offset: usize,
        writer: &mut VmWriter,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        if !offset.is_multiple_of(PAGEMAP_ENTRY_SIZE)
            || !writer.avail().is_multiple_of(PAGEMAP_ENTRY_SIZE)
        {
            return_errno_with_message!(
                Errno::EINVAL,
                "pagemap reads must be aligned to the entry size"
            );
        }

        let Some(process) = self.0.process() else {
            return Ok(0);
        };
        let vmar_guard = process.lock_vmar();
        if !vmar_guard.is_same_as(&self.1) {
            return Ok(0);
        }
        let Some(vmar) = vmar_guard.as_ref() else {
            return Ok(0);
        };

        let first_page = offset / PAGEMAP_ENTRY_SIZE;
        let max_pages = VMAR_CAP_ADDR / PAGE_SIZE;
        if first_page >= max_pages {
            return Ok(0);
        }

        let requested_pages = writer.avail() / PAGEMAP_ENTRY_SIZE;
        let pages_to_read = requested_pages.min(max_pages - first_page);
        let mut pages_read = 0;

        while pages_read < pages_to_read {
            let page_index = first_page + pages_read;
            let page_addr = page_index
                .checked_mul(PAGE_SIZE)
                .ok_or_else(|| Error::with_message(Errno::EINVAL, "page address overflows"))?;
            let entry = pagemap_entry(vmar.is_page_present(page_addr)?);
            writer.write_val(&entry)?;
            pages_read += 1;
        }

        Ok(pages_read * PAGEMAP_ENTRY_SIZE)
    }

    fn write_at(
        &self,
        _offset: usize,
        _reader: &mut VmReader,
        _status_flags: StatusFlags,
    ) -> Result<usize> {
        return_errno_with_message!(Errno::EPERM, "`/proc/[pid]/pagemap` is not writable");
    }
}

impl PerOpenFileOps for PagemapFileHandle {
    fn check_seekable(&self) -> Result<()> {
        Ok(())
    }

    fn is_offset_aware(&self) -> bool {
        true
    }
}

fn pagemap_entry(is_present: bool) -> u64 {
    // PFNs remain masked. This preserves the security boundary used by Linux for
    // callers without CAP_SYS_ADMIN while exposing residency information.
    if is_present { PAGEMAP_PRESENT } else { 0 }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::{PAGEMAP_PRESENT, pagemap_entry};

    #[ktest]
    fn pagemap_entry_reports_only_actual_presence() {
        assert_eq!(pagemap_entry(false), 0);
        assert_eq!(pagemap_entry(true), PAGEMAP_PRESENT);
    }
}
