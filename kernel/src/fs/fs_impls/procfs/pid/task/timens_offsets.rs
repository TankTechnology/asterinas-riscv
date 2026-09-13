// SPDX-License-Identifier: MPL-2.0

use aster_util::printer::VmPrinter;

use super::TidDirOps;
use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
    process::posix_thread::AsPosixThread,
    thread::Thread,
};

/// The writable offsets for the time namespace used by future children.
pub struct TimeNsOffsetsFileOps {
    dir: TidDirOps,
}

impl TimeNsOffsetsFileOps {
    pub fn new_inode(dir: &TidDirOps, parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self { dir: dir.clone() }, parent, mkmod!(a+r, u+w))
    }

    fn owner(&self) -> Result<Arc<Thread>> {
        self.dir
            .thread()
            .ok_or_else(|| Error::with_message(Errno::ESRCH, "the thread does not exist"))
    }
}

impl ProcFileOps for TimeNsOffsetsFileOps {
    fn owner_thread(&self) -> Option<Arc<Thread>> {
        self.dir.thread()
    }

    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let thread = self.owner()?;
        let ns_proxy = thread.as_posix_thread().unwrap().ns_proxy().lock();
        let ns_proxy = ns_proxy
            .as_ref()
            .ok_or_else(|| Error::with_message(Errno::ESRCH, "the thread has exited"))?;
        let (mono_sec, mono_nsec, boot_sec, boot_nsec) = ns_proxy.time_ns_for_children().offsets();
        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(printer, "monotonic {} {}", mono_sec, mono_nsec)?;
        writeln!(printer, "boottime {} {}", boot_sec, boot_nsec)?;
        Ok(printer.bytes_written())
    }

    fn write_at(&self, offset: usize, reader: &mut VmReader) -> Result<usize> {
        if offset != 0 {
            return_errno_with_message!(Errno::EINVAL, "time namespace writes must start at zero");
        }
        let (input, read_bytes) = reader.read_cstring_until_end(PAGE_SIZE - 1)?;
        let text = input
            .to_str()
            .map_err(|_| Error::with_message(Errno::EINVAL, "offset is not valid UTF-8"))?;
        let mut fields = text.split_whitespace();
        let clock = match fields.next() {
            Some("monotonic") | Some("1") => 1,
            Some("boottime") | Some("7") => 7,
            _ => return_errno_with_message!(Errno::EINVAL, "invalid time namespace clock"),
        };
        let sec = fields
            .next()
            .and_then(|value| value.parse::<i64>().ok())
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "invalid seconds offset"))?;
        let nsec = fields
            .next()
            .and_then(|value| value.parse::<i64>().ok())
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "invalid nanoseconds offset"))?;
        if fields.next().is_some() {
            return_errno_with_message!(Errno::EINVAL, "too many time namespace offset fields");
        }

        let thread = self.owner()?;
        let ns_proxy = thread.as_posix_thread().unwrap().ns_proxy().lock();
        let ns_proxy = ns_proxy
            .as_ref()
            .ok_or_else(|| Error::with_message(Errno::ESRCH, "the thread has exited"))?;
        ns_proxy
            .time_ns_for_children()
            .set_offset(clock, sec, nsec)?;
        Ok(read_bytes)
    }
}
