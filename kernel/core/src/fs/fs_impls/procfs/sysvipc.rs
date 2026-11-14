// SPDX-License-Identifier: MPL-2.0

//! This module offers `/proc/sysvipc/{shm,sem,msg}`, the tables `ipcs` reads
//! to enumerate System V IPC objects.
//!
//! Reference: <https://man7.org/linux/man-pages/man5/proc_sysvipc.5.html>
//!
//! The columns are Linux's. Two of them are not tracked here and are printed
//! as zero: `rss`/`swap` for shared memory (this kernel does not keep a
//! per-segment resident set) and the whole `msg` table (System V message
//! queues are not implemented).

use aster_util::printer::VmPrinter;

use crate::{
    fs::{
        file::{InodeType, mkmod},
        procfs::template::{
            ProcDir, ProcDirOps, ProcFile, ProcFileOps, ReaddirEntry, listed_entries_from_table,
            lookup_child_from_table, visit_listed_entries,
        },
        vfs::inode::Inode,
    },
    prelude::*,
    process::posix_thread::AsPosixThread,
};

/// The inode at `/proc/sysvipc`.
pub struct SysvipcDirOps;

impl SysvipcDirOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        ProcDir::new(Self, parent, mkmod!(a+rx))
    }

    const STATIC_ENTRIES: &'static [(
        &'static str,
        InodeType,
        fn(Weak<dyn Inode>) -> Arc<dyn Inode>,
    )] = &[
        ("shm", InodeType::File, |parent| {
            ProcFile::new(
                SysvipcFileOps {
                    table: SysvipcTable::SharedMemory,
                },
                parent,
                mkmod!(a+r),
            )
        }),
        ("sem", InodeType::File, |parent| {
            ProcFile::new(
                SysvipcFileOps {
                    table: SysvipcTable::Semaphores,
                },
                parent,
                mkmod!(a+r),
            )
        }),
        ("msg", InodeType::File, |parent| {
            ProcFile::new(
                SysvipcFileOps {
                    table: SysvipcTable::MessageQueues,
                },
                parent,
                mkmod!(a+r),
            )
        }),
    ];
}

impl ProcDirOps for SysvipcDirOps {
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
enum SysvipcTable {
    SharedMemory,
    Semaphores,
    MessageQueues,
}

struct SysvipcFileOps {
    table: SysvipcTable,
}

impl ProcFileOps for SysvipcFileOps {
    fn read_at(&self, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);

        let thread = current_thread!();
        let posix_thread = thread.as_posix_thread().unwrap();
        let ns_proxy = posix_thread.ns_proxy().lock();
        let ipc_ns = ns_proxy
            .as_ref()
            .ok_or_else(|| Error::with_message(Errno::ENOENT, "the thread has no namespaces"))?
            .ipc_ns()
            .clone();

        match self.table {
            SysvipcTable::SharedMemory => {
                writeln!(
                    printer,
                    "       key      shmid perms       size  cpid  lpid nattch   uid   gid  cuid  cgid      atime      dtime      ctime       rss      swap"
                )?;
                for row in ipc_ns.shm_proc_rows() {
                    writeln!(
                        printer,
                        "{:>10} {:>10} {:>5} {:>10} {:>5} {:>5} {:>6} {:>5} {:>5} {:>5} {:>5} {:>10} {:>10} {:>10} {:>9} {:>9}",
                        row.key,
                        row.id,
                        row.mode,
                        row.size,
                        row.cpid,
                        // `lpid` is the last process that attached or detached;
                        // this kernel does not track it per segment.
                        0,
                        row.nattch,
                        row.uid,
                        row.gid,
                        row.cuid,
                        row.cgid,
                        row.atime,
                        row.dtime,
                        row.ctime,
                        0,
                        0
                    )?;
                }
            }
            SysvipcTable::Semaphores => {
                writeln!(
                    printer,
                    "       key      semid perms      nsems   uid   gid  cuid  cgid      otime      ctime"
                )?;
                for row in ipc_ns.sem_proc_rows() {
                    writeln!(
                        printer,
                        "{:>10} {:>10} {:>5} {:>10} {:>5} {:>5} {:>5} {:>5} {:>10} {:>10}",
                        row.key,
                        row.id,
                        row.mode,
                        row.nsems,
                        row.uid,
                        row.gid,
                        row.cuid,
                        row.cgid,
                        row.otime,
                        row.ctime
                    )?;
                }
            }
            SysvipcTable::MessageQueues => {
                // System V message queues are not implemented, so the table is
                // empty but present: `ipcs -q` prints "no queues" rather than
                // failing to open the file.
                writeln!(
                    printer,
                    "       key      msqid perms      cbytes    qnum lspid lrpid   uid   gid  cuid  cgid      stime      rtime      ctime"
                )?;
            }
        }

        Ok(printer.bytes_written())
    }
}
