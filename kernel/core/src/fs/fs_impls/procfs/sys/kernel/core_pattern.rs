// SPDX-License-Identifier: MPL-2.0

//! `/proc/sys/kernel/core_pattern` — the core-dump name pattern.
//!
//! The value is stored and exposed exactly like Linux's, so configuration
//! tools such as `systemd-sysctl` succeed. Core dumping itself is not
//! implemented yet (see `syscall/prctl.rs`), so the pattern has no effect on
//! the kernel's behavior today.

use aster_util::printer::VmPrinter;
use ostd::sync::SpinLock;

use crate::{
    fs::{
        file::mkmod,
        procfs::template::{ProcFile, ProcFileOps},
        vfs::inode::Inode,
    },
    prelude::*,
};

/// Linux's `CORENAME_MAX_SIZE`.
const CORE_PATTERN_MAX: usize = 128;

static CORE_PATTERN: SpinLock<[u8; CORE_PATTERN_MAX]> = SpinLock::new({
    let mut pattern = [0u8; CORE_PATTERN_MAX];
    pattern[0] = b'c';
    pattern[1] = b'o';
    pattern[2] = b'r';
    pattern[3] = b'e';
    pattern
});

/// Represents the inode at `/proc/sys/kernel/core_pattern`.
pub(super) struct CorePatternFileOps;

impl CorePatternFileOps {
    pub(super) fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference: <https://elixir.bootlin.com/linux/v6.16.5/source/fs/coredump.c#L79>
        ProcFile::new(Self, parent, mkmod!(a+r, u+w))
    }
}

impl ProcFileOps for CorePatternFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        // Copy the pattern out before touching user memory: the reader/writer
        // may fault, and a spinlock must not be held across that.
        let pattern = *CORE_PATTERN.lock();
        let len = pattern
            .iter()
            .position(|&byte| byte == 0)
            .unwrap_or(CORE_PATTERN_MAX);

        let mut printer = VmPrinter::new_skip(writer, offset);
        printer.write_bytes(&pattern[..len])?;
        printer.write_bytes(b"\n")?;
        Ok(printer.bytes_written())
    }

    fn write_at(&self, offset: usize, reader: &mut VmReader) -> Result<usize> {
        if offset != 0 {
            return_errno_with_message!(Errno::EINVAL, "core_pattern must be written from offset 0");
        }

        let len = reader.remain();
        let mut buf = [0u8; CORE_PATTERN_MAX];
        // Leave room for the terminating NUL, like Linux's CORENAME_MAX_SIZE.
        let copy_len = len.min(CORE_PATTERN_MAX - 1);
        let mut writer = VmWriter::from(&mut buf[..copy_len]).to_fallible();
        let copied = reader.read_fallible(&mut writer)?;
        reader.skip(len - copied);

        // sysctl strings drop trailing whitespace, including the newline that
        // `sysctl` and `systemd-sysctl` append.
        let end = buf[..copied]
            .iter()
            .rposition(|byte| !byte.is_ascii_whitespace())
            .map(|index| index + 1)
            .unwrap_or(0);

        let mut pattern = CORE_PATTERN.lock();
        pattern.fill(0);
        pattern[..end].copy_from_slice(&buf[..end]);
        Ok(len)
    }
}
