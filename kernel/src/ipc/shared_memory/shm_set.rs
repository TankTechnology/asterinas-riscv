// SPDX-License-Identifier: MPL-2.0

use core::sync::atomic::{AtomicU32, AtomicU64, Ordering};

use aster_rights::ReadOp;

use crate::{
    ipc::{IpcKey, IpcPerm, IpcPermission},
    prelude::*,
    process::{Credentials, Pid},
    time::clocks::RealTimeCoarseClock,
    vm::page_cache::{Vmo, VmoOptions},
};

// The following constant values are derived from the default values in Linux.

/// Maximum number of shared memory segments system-wide.
pub const SHMMNI: usize = 4096;
/// Minimum size of a shared memory segment (in bytes).
pub const SHMMIN: usize = 1;
/// Maximum size of a shared memory segment (in bytes).
///
/// Mirrors Linux's 64-bit `SHMMAX` default of `ULONG_MAX - (1UL << 24)`.
pub const SHMMAX: usize = usize::MAX - (1 << 24);

/// A System V shared memory segment.
pub struct ShmSet {
    /// Size of the segment (in bytes).
    size: usize,
    /// The anonymous VMO backing the segment's pages.
    vmo: Arc<Vmo>,
    /// Segment permission.
    permission: IpcPermission,
    /// Creation or last modification time via `shmctl`.
    shm_ctime: AtomicU64,
    /// Last attach (`shmat`) time.
    shm_atime: AtomicU64,
    /// Last detach (`shmdt`) time.
    shm_dtime: AtomicU64,
    /// PID of the creator.
    shm_cpid: Pid,
    /// PID of the last process to attach or detach.
    shm_lpid: AtomicU32,
    /// Number of current attaches.
    nattch: AtomicU32,
}

// In Linux, the `shmid_ds` layout differs between x86_64 and the other
// 64-bit architectures (which use the generic `shmid64_ds` layout).
// Reference: <https://elixir.bootlin.com/linux/v6.16.9/A/ident/shmid64_ds>.

#[cfg(target_arch = "x86_64")]
#[padding_struct]
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub struct ShmidDs {
    shm_perm: IpcPerm,
    shm_segsz: u64,
    shm_atime: u64,
    _unused1: u64,
    shm_dtime: u64,
    _unused2: u64,
    shm_ctime: u64,
    _unused3: u64,
    shm_cpid: u32,
    shm_lpid: u32,
    shm_nattch: u64,
    _unused4: u64,
    _unused5: u64,
}

#[cfg(not(target_arch = "x86_64"))]
#[padding_struct]
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Pod)]
pub struct ShmidDs {
    shm_perm: IpcPerm,
    shm_segsz: u64,
    shm_atime: u64,
    shm_dtime: u64,
    shm_ctime: u64,
    shm_cpid: u32,
    shm_lpid: u32,
    shm_nattch: u64,
    _unused4: u64,
    _unused5: u64,
}

impl ShmSet {
    pub fn size(&self) -> usize {
        self.size
    }

    pub fn vmo(&self) -> &Arc<Vmo> {
        &self.vmo
    }

    pub fn permission(&self) -> &IpcPermission {
        &self.permission
    }

    /// Records an attach.
    pub fn attach(&self, pid: Pid) {
        self.nattch.fetch_add(1, Ordering::AcqRel);
        self.shm_lpid.store(pid, Ordering::Relaxed);
        self.update_atime();
    }

    /// Records a detach.
    pub fn detach(&self, pid: Pid) {
        self.nattch.fetch_sub(1, Ordering::AcqRel);
        self.shm_lpid.store(pid, Ordering::Relaxed);
        self.update_dtime();
    }

    fn update_atime(&self) {
        self.shm_atime.store(
            RealTimeCoarseClock::get().read_time().as_secs(),
            Ordering::Relaxed,
        );
    }

    fn update_dtime(&self) {
        self.shm_dtime.store(
            RealTimeCoarseClock::get().read_time().as_secs(),
            Ordering::Relaxed,
        );
    }

    pub fn shmid_ds(&self) -> ShmidDs {
        let ipc_perm = IpcPerm {
            key: self.permission.key().cast_unsigned(),
            uid: self.permission.uid().into(),
            gid: self.permission.gid().into(),
            cuid: self.permission.cuid().into(),
            cgid: self.permission.cguid().into(),
            mode: self.permission.mode(),
            ..IpcPerm::default()
        };

        ShmidDs {
            shm_perm: ipc_perm,
            shm_segsz: self.size as u64,
            shm_atime: self.shm_atime.load(Ordering::Relaxed),
            shm_dtime: self.shm_dtime.load(Ordering::Relaxed),
            shm_ctime: self.shm_ctime.load(Ordering::Relaxed),
            shm_cpid: self.shm_cpid,
            shm_lpid: self.shm_lpid.load(Ordering::Relaxed),
            shm_nattch: self.nattch.load(Ordering::Relaxed) as u64,
            ..ShmidDs::default()
        }
    }

    pub(in crate::ipc) fn new(
        key: IpcKey,
        size: usize,
        mode: u16,
        pid: Pid,
        credentials: &Credentials<ReadOp>,
    ) -> Result<Self> {
        if !(SHMMIN..=SHMMAX).contains(&size) {
            return_errno_with_message!(Errno::EINVAL, "the segment size is out of bounds");
        }

        let vmo = VmoOptions::new(size).alloc()?;
        let permission =
            IpcPermission::new_shm_perm(key, credentials.euid(), credentials.egid(), mode);

        Ok(Self {
            size,
            vmo,
            permission,
            shm_ctime: AtomicU64::new(RealTimeCoarseClock::get().read_time().as_secs()),
            shm_atime: AtomicU64::new(0),
            shm_dtime: AtomicU64::new(0),
            shm_cpid: pid,
            shm_lpid: AtomicU32::new(0),
            nattch: AtomicU32::new(0),
        })
    }
}
