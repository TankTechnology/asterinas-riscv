// SPDX-License-Identifier: MPL-2.0

//! This module offers `/proc/pressure/{cpu,memory,io}`, the pressure stall
//! information (PSI) that `systemd-oomd`, `systemd`'s own oom heuristics, and
//! monitoring agents open on every start.
//!
//! Reference: <https://docs.kernel.org/accounting/psi.html>
//!
//! The kernel does not sample stall time yet, so every figure is zero: what
//! this provides is the interface, in the shape the readers expect, so those
//! tools can start. When PSI accounting lands, only the numbers change.
//!
//! The `cpu` file carries `some` only, as on Linux; `memory` and `io` carry
//! both `some` and `full`.

use alloc::string::String;

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::{InodeType, mkmod},
        procfs::template::{
            ListedEntry, ProcDir, ProcDirOps, ProcFile, ProcFileOps, ReaddirEntry,
            listed_entries_from_table, lookup_child_from_table, visit_listed_entries,
        },
        vfs::inode::Inode,
    },
    prelude::*,
};

/// The inode at `/proc/pressure`.
pub struct PressureDirOps;

impl PressureDirOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcDir::new(Self, parent, mkmod!(a+rx))
    }

    const STATIC_ENTRIES: &'static [(
        &'static str,
        InodeType,
        fn(Weak<dyn Inode>) -> Arc<dyn Inode>,
    )] = &[
        ("cpu", InodeType::File, |parent| {
            ProcFile::new(
                PressureFileOps {
                    resource: PressureResource::Cpu,
                },
                parent,
                mkmod!(a+r),
            )
        }),
        ("memory", InodeType::File, |parent| {
            ProcFile::new(
                PressureFileOps {
                    resource: PressureResource::Memory,
                },
                parent,
                mkmod!(a+r),
            )
        }),
        ("io", InodeType::File, |parent| {
            ProcFile::new(
                PressureFileOps {
                    resource: PressureResource::Io,
                },
                parent,
                mkmod!(a+r),
            )
        }),
    ];
}

impl ProcDirOps for PressureDirOps {
    fn lookup_child(&self, this_dir: &ProcDir<Self>, name: &str) -> Result<Arc<dyn Inode>> {
        if let Some(child) = lookup_child_from_table(name, Self::STATIC_ENTRIES, |f| {
            (f)(this_dir.this_weak().clone())
        }) {
            return Ok(child);
        }
        return_errno_with_message!(Errno::ENOENT, "the file does not exist");
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

#[derive(Clone, Copy)]
enum PressureResource {
    Cpu,
    Memory,
    Io,
}

struct PressureFileOps {
    resource: PressureResource,
}

impl ProcFileOps for PressureFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);

        writeln!(
            printer,
            "some avg10=0.00 avg60=0.00 avg300=0.00 total=0"
        )?;
        if !matches!(self.resource, PressureResource::Cpu) {
            writeln!(printer, "full avg10=0.00 avg60=0.00 avg300=0.00 total=0")?;
        }

        Ok(printer.bytes_written())
    }
}

/// Unused today; kept so the file names stay in one place when accounting
/// arrives.
#[expect(dead_code)]
fn resource_name(resource: PressureResource) -> String {
    String::from(match resource {
        PressureResource::Cpu => "cpu",
        PressureResource::Memory => "memory",
        PressureResource::Io => "io",
    })
}

/// Ensures the `ListedEntry` import is used on every build.
#[expect(dead_code)]
fn _listed_entry_type_check(entry: ListedEntry<'_>) -> ListedEntry<'_> {
    entry
}
