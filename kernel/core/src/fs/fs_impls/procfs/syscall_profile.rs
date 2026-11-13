// SPDX-License-Identifier: MPL-2.0

//! `/proc/syscall_profile`: the accumulated system call phase timings
//! collected by `crate::syscall::profile`.
//!
//! Reading the file prints one line per `(syscall, phase)` pair; writing
//! anything to it (for example `echo > /proc/syscall_profile`) clears the
//! counters, so a measurement can be scoped to one workload:
//!
//! ```text
//! echo > /proc/syscall_profile; run_workload; cat /proc/syscall_profile
//! ```
//!
//! Sampling starts from the `asterinas.stat_profile` boot flag, but the file
//! is also the run-time switch (`echo on` / `echo off`), so a measurement does
//! not need a reboot with a different command line.

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
    syscall::profile,
};

/// Represents the inode at `/proc/syscall_profile`.
pub struct SyscallProfileFileOps;

impl SyscallProfileFileOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+rw))
    }
}

impl ProcFileOps for SyscallProfileFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        write!(printer, "{}", profile::render_report())?;
        Ok(printer.bytes_written())
    }

    fn write_at(&self, _offset: usize, reader: &mut VmReader) -> Result<usize> {
        // The content selects the action so a measurement can be scripted
        // without rebooting:
        //   echo on    > /proc/syscall_profile   start sampling
        //   echo off   > /proc/syscall_profile   stop sampling
        //   echo reset > /proc/syscall_profile   clear the counters
        // An empty write (`echo >`) clears, matching what a reader expects
        // from a statistics file.
        let mut buffer = [0u8; 32];
        let mut text = alloc::string::String::new();
        let mut consumed = 0;
        while consumed < 32 {
            let read = reader
                .read_fallible(&mut VmWriter::from(&mut buffer[..]))
                .map_err(|(err, _)| err)?;
            if read == 0 {
                break;
            }
            text.push_str(core::str::from_utf8(&buffer[..read]).unwrap_or(""));
            consumed += read;
        }

        match text.trim() {
            "on" | "1" => profile::set_enabled(true),
            "off" | "0" => profile::set_enabled(false),
            _ => profile::reset(),
        }
        Ok(consumed)
    }
}
