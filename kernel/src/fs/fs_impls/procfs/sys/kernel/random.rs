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

    const STATIC_ENTRIES: &'static [StaticEntry] =
        &[("boot_id", InodeType::File, BootIdFileOps::new_inode)];
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

impl BootIdFileOps {
    fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self, parent, mkmod!(a+r))
    }
}

impl ProcFileOps for BootIdFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        static BOOT_ID: Once<[u8; 16]> = Once::new();

        let boot_id = BOOT_ID.call_once(|| {
            let mut value = [0; 16];
            getrandom(&mut value);
            value[6] = (value[6] & 0x0f) | 0x40;
            value[8] = (value[8] & 0x3f) | 0x80;
            value
        });
        let mut printer = VmPrinter::new_skip(writer, offset);
        for (index, byte) in boot_id.iter().enumerate() {
            if matches!(index, 4 | 6 | 8 | 10) {
                write!(printer, "-")?;
            }
            write!(printer, "{byte:02x}")?;
        }
        writeln!(printer)?;
        Ok(printer.bytes_written())
    }
}
