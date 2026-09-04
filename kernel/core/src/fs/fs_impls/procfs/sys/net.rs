// SPDX-License-Identifier: MPL-2.0

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::{InodeType, mkmod},
        procfs::{
            StaticEntry,
            template::{
                ProcDir, ProcDirOps, ProcFile, ProcFileOps, ReaddirEntry,
                listed_entries_from_table, lookup_child_from_table, read_i32_from,
                visit_listed_entries,
            },
        },
        vfs::inode::Inode,
    },
    net::net_ns::current_net_ns,
    prelude::*,
};

macro_rules! static_dir {
    ($name:ident, $entries:expr) => {
        pub struct $name;

        impl $name {
            pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
                ProcDir::new(Self, parent, mkmod!(a+rx))
            }

            const STATIC_ENTRIES: &'static [StaticEntry] = $entries;
        }

        impl ProcDirOps for $name {
            fn lookup_child(&self, this_dir: &ProcDir<Self>, name: &str) -> Result<Arc<dyn Inode>> {
                lookup_child_from_table(name, Self::STATIC_ENTRIES, |factory| {
                    (factory)(this_dir.this_weak().clone())
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
    };
}

static_dir!(
    NetDirOps,
    &[("ipv4", InodeType::Dir, Ipv4DirOps::new_inode)]
);
static_dir!(
    Ipv4DirOps,
    &[("conf", InodeType::Dir, ConfDirOps::new_inode)]
);
static_dir!(
    ConfDirOps,
    &[
        ("default", InodeType::Dir, DefaultDirOps::new_inode),
        ("lo", InodeType::Dir, LoopbackDirOps::new_inode),
    ]
);
static_dir!(
    DefaultDirOps,
    &[("tag", InodeType::File, TagFileOps::new_default_inode)]
);
static_dir!(
    LoopbackDirOps,
    &[("tag", InodeType::File, TagFileOps::new_loopback_inode)]
);

#[derive(Clone, Copy)]
enum TagKind {
    Default,
    Loopback,
}

struct TagFileOps(TagKind);

impl TagFileOps {
    fn new_default_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self(TagKind::Default), parent, mkmod!(a+r, u+w))
    }

    fn new_loopback_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcFile::new(Self(TagKind::Loopback), parent, mkmod!(a+r, u+w))
    }
}

impl ProcFileOps for TagFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let net_ns = current_net_ns();
        let value = match self.0 {
            TagKind::Default => net_ns.default_ipv4_tag(),
            TagKind::Loopback => net_ns.loopback_ipv4_tag(),
        };
        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(printer, "{}", value)?;
        Ok(printer.bytes_written())
    }

    fn write_at(&self, offset: usize, reader: &mut VmReader) -> Result<usize> {
        if offset != 0 {
            return_errno_with_message!(Errno::EINVAL, "sysctl writes must start at offset zero");
        }
        let (value, read_bytes) = read_i32_from(reader)?;
        let net_ns = current_net_ns();
        match self.0 {
            TagKind::Default => net_ns.set_default_ipv4_tag(value),
            TagKind::Loopback => net_ns.set_loopback_ipv4_tag(value),
        }
        Ok(read_bytes)
    }
}
