// SPDX-License-Identifier: MPL-2.0

use aster_util::printer::VmPrinter;
use spin::Once;

use crate::{
    fs::{
        file::{InodeType, mkmod},
        procfs::{
            ProcDir, StaticEntry,
            template::{
                ProcDirOps, ProcFile, ProcFileOps, ReaddirEntry, listed_entries_from_table,
                lookup_child_from_table, visit_listed_entries,
            },
        },
        vfs::inode::Inode,
    },
    prelude::*,
    util::random::getrandom,
};

/// Represents the inode at `/proc/sys/kernel/random`.
pub struct RandomDirOps;

impl RandomDirOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcDir::new(Self, parent, mkmod!(a+rx))
    }

    const STATIC_ENTRIES: &'static [StaticEntry] = &[
        ("boot_id", InodeType::File, BootIdFileOps::new_inode),
        ("uuid", InodeType::File, UuidFileOps::new_inode),
    ];
}

impl ProcDirOps for RandomDirOps {
    fn lookup_child(&self, this_dir: &ProcDir<Self>, name: &str) -> Result<Arc<dyn Inode>> {
        lookup_child_from_table(name, Self::STATIC_ENTRIES, |new_inode| {
            new_inode(this_dir.this_weak().clone())
        })
        .ok_or_else(|| Error::with_message(Errno::ENOENT, "the file does not exist"))
    }

    fn visit_entries_from_offset<'a, F>(&'a self, offset: usize, visit_fn: F) -> Result<()>
    where
        F: FnMut(ReaddirEntry<'a>) -> Result<()>,
    {
        visit_listed_entries(
            offset,
            listed_entries_from_table(Self::STATIC_ENTRIES),
            visit_fn,
        )
    }
}

struct BootIdFileOps;

fn new_uuid() -> [u8; 16] {
    let mut value = [0; 16];
    getrandom(&mut value);
    value[6] = (value[6] & 0x0f) | 0x40;
    value[8] = (value[8] & 0x3f) | 0x80;
    value
}

fn uuid_text(value: &[u8; 16]) -> [u8; 37] {
    const HEX: &[u8; 16] = b"0123456789abcdef";

    let mut output = [0; 37];
    let mut cursor = 0;
    for (index, byte) in value.iter().enumerate() {
        if matches!(index, 4 | 6 | 8 | 10) {
            output[cursor] = b'-';
            cursor += 1;
        }
        output[cursor] = HEX[(byte >> 4) as usize];
        output[cursor + 1] = HEX[(byte & 0x0f) as usize];
        cursor += 2;
    }
    output[36] = b'\n';
    output
}

impl BootIdFileOps {
    fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for BootIdFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        static BOOT_ID: Once<[u8; 16]> = Once::new();

        let boot_id = BOOT_ID.call_once(new_uuid);
        let mut printer = VmPrinter::new_skip(writer, offset);
        printer.write_bytes(&uuid_text(boot_id))?;
        Ok(printer.bytes_written())
    }
}

struct UuidFileOps;

impl UuidFileOps {
    fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for UuidFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        printer.write_bytes(&uuid_text(&new_uuid()))?;
        Ok(printer.bytes_written())
    }
}
