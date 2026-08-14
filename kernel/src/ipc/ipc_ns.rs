// SPDX-License-Identifier: MPL-2.0

//! Defines the IPC namespace abstraction.
//!
//! An IPC namespace isolates System V IPC resources from other namespaces.
//! It currently manages semaphore sets only, while message queues and shared
//! memory remain to be added.
//!
//! Each namespace stores its semaphore sets in a per-namespace map keyed by
//! IPC key and uses a dedicated ID allocator to assign semaphore identifiers.

use aster_rights::ReadOp;
use spin::Once;

use super::{
    IPC_PRIVATE, IpcFlags, IpcId, IpcKey,
    ipc_ids::IpcIds,
    semaphore::system_v::{
        PermissionMode,
        sem_set::{SEMMNI, SemaphoreSet},
    },
    shared_memory::{
        PermissionMode as ShmPermissionMode,
        shm_set::{SHMMNI, ShmSet},
    },
};
use crate::{
    fs::pseudofs::{NsCommonOps, NsType, StashedDentry},
    prelude::*,
    process::{
        Credentials, Pid, UserNamespace, credentials::capabilities::CapSet,
        posix_thread::PosixThread,
    },
    security::lsm::hooks as lsm_hooks,
};

/// The IPC namespace.
///
/// An IPC namespace isolates System V IPC objects
/// (semaphores, message queues, shared memory).
/// Each namespace maintains its own independent set
/// of IPC resources and identifier allocator.
///
/// Lock ordering:
/// `sem_ids` -> `SemaphoreSet::inner`.
pub struct IpcNamespace {
    /// Semaphore sets within this namespace.
    sem_ids: IpcIds<SemaphoreSet>,
    /// Shared memory segments within this namespace.
    shm_ids: IpcIds<ShmSet>,
    /// Attachments of shared memory segments, keyed by `(pid, address)`.
    shm_attachments: RwMutex<BTreeMap<(Pid, Vaddr), IpcId>>,
    /// Owner user namespace.
    owner: Arc<UserNamespace>,
    /// Stashed dentry for nsfs.
    stashed_dentry: StashedDentry,
}

impl IpcNamespace {
    /// Returns a reference to the singleton initial IPC namespace.
    pub fn get_init_singleton() -> &'static Arc<IpcNamespace> {
        static INIT: Once<Arc<IpcNamespace>> = Once::new();

        INIT.call_once(|| {
            let owner = UserNamespace::get_init_singleton().clone();
            Self::new(owner)
        })
    }

    fn new(owner: Arc<UserNamespace>) -> Arc<Self> {
        const MAX_SEM_ID: IpcId = {
            assert!(SEMMNI <= u32::MAX as usize);
            IpcId::new(SEMMNI as u32)
        };
        const MAX_SHM_ID: IpcId = {
            assert!(SHMMNI <= u32::MAX as usize);
            IpcId::new(SHMMNI as u32)
        };

        let sem_ids = IpcIds::new(MAX_SEM_ID);
        let shm_ids = IpcIds::new(MAX_SHM_ID);
        let stashed_dentry = StashedDentry::new();

        Arc::new(Self {
            sem_ids,
            shm_ids,
            shm_attachments: RwMutex::new(BTreeMap::new()),
            owner,
            stashed_dentry,
        })
    }

    /// Clones a new IPC namespace from `self`.
    ///
    /// The new namespace starts with an empty set of IPC resources.
    pub fn new_clone(
        &self,
        owner: Arc<UserNamespace>,
        posix_thread: &PosixThread,
    ) -> Result<Arc<Self>> {
        lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
            owner.as_ref(),
            posix_thread,
            CapSet::SYS_ADMIN,
        ))?;
        Ok(Self::new(owner))
    }

    /// Calls `op` with the semaphore set identified by `semid`.
    pub fn with_sem_set<T, F>(
        &self,
        semid: IpcId,
        required_perm: PermissionMode,
        op: F,
    ) -> Result<T>
    where
        F: FnOnce(&SemaphoreSet) -> Result<T>,
    {
        self.sem_ids.with(semid, |sem_set| {
            Self::validate_sem_set(sem_set, required_perm)?;
            op(sem_set)
        })?
    }

    /// Removes the semaphore set identified by `semid`.
    pub fn remove_sem_set<F>(&self, semid: IpcId, may_remove: F) -> Result<()>
    where
        F: FnOnce(&SemaphoreSet) -> Result<()>,
    {
        self.sem_ids.remove(semid, may_remove)
    }

    /// Returns the existing semaphore set or creates a new one.
    pub fn get_or_create_sem_set(
        &self,
        key: IpcKey,
        num_sems: usize,
        flags: IpcFlags,
        mode: u16,
        credentials: Credentials<ReadOp>,
    ) -> Result<IpcId> {
        if key == IPC_PRIVATE {
            return self.create_sem_set(num_sems, mode, credentials);
        }

        // For now, we compute `semid` by hashing `key`. If the hash conflicts, we will simply
        // return an error. See the TODO below.
        const { assert!(SEMMNI <= u32::MAX as usize) };
        let semid = IpcId::new(key.cast_unsigned() % SEMMNI as u32 + 1);

        loop {
            match self.sem_ids.with(semid, |sem_set| {
                if sem_set.permission().key() != key {
                    if flags.contains(IpcFlags::IPC_CREAT) {
                        // TODO: Manage all keys in a data structure (e.g., a map)
                        return_errno_with_message!(Errno::ENOSPC, "key hashes conflict");
                    }
                    return_errno_with_message!(Errno::ENOENT, "the key does not exist");
                }

                Self::validate_sem_set(sem_set, PermissionMode::ALTER | PermissionMode::READ)?;

                if flags.contains(IpcFlags::IPC_CREAT | IpcFlags::IPC_EXCL) {
                    return_errno_with_message!(
                        Errno::EEXIST,
                        "the semaphore set already exists with IPC_EXCL"
                    );
                }

                if sem_set.num_sems() < num_sems {
                    return_errno_with_message!(Errno::EINVAL, "the semaphore set is too small");
                }

                Ok(semid)
            }) {
                Err(_id_not_exist) if flags.contains(IpcFlags::IPC_CREAT) => {}
                Err(_id_not_exist) => {
                    return_errno_with_message!(Errno::ENOENT, "the key does not exist");
                }
                Ok(result) => return result,
            }

            match self.sem_ids.insert_at(semid, |_| {
                SemaphoreSet::new(key, num_sems, mode, &credentials)
            }) {
                Ok(()) => return Ok(semid),
                Err(err) if err.error() == Errno::EEXIST => continue,
                Err(err) => return Err(err),
            }
        }
    }

    fn validate_sem_set(_sem_set: &SemaphoreSet, required_perm: PermissionMode) -> Result<()> {
        if !required_perm.is_empty() {
            // TODO: Support permission check
            warn!("Semaphore doesn't support permission check now");
        }

        Ok(())
    }

    /// Creates a new semaphore set and returns its ID.
    fn create_sem_set(
        &self,
        num_sems: usize,
        mode: u16,
        credentials: Credentials<ReadOp>,
    ) -> Result<IpcId> {
        self.sem_ids
            .insert_auto(|_| SemaphoreSet::new(IPC_PRIVATE, num_sems, mode, &credentials))
    }

    /// Calls `op` with the shared memory segment identified by `shmid`.
    pub fn with_shm_set<T, F>(
        &self,
        shmid: IpcId,
        required_perm: ShmPermissionMode,
        op: F,
    ) -> Result<T>
    where
        F: FnOnce(&ShmSet) -> Result<T>,
    {
        self.shm_ids.with(shmid, |shm_set| {
            Self::validate_shm_set(shm_set, required_perm)?;
            op(shm_set)
        })?
    }

    /// Removes the shared memory segment identified by `shmid`.
    pub fn remove_shm_set<F>(&self, shmid: IpcId, may_remove: F) -> Result<()>
    where
        F: FnOnce(&ShmSet) -> Result<()>,
    {
        self.shm_ids.remove(shmid, may_remove)
    }

    /// Returns the existing shared memory segment or creates a new one.
    pub fn get_or_create_shm_set(
        &self,
        key: IpcKey,
        size: usize,
        flags: IpcFlags,
        mode: u16,
        pid: Pid,
        credentials: Credentials<ReadOp>,
    ) -> Result<IpcId> {
        if key == IPC_PRIVATE {
            return self.create_shm_set(size, mode, pid, credentials);
        }

        // For now, we compute `shmid` by hashing `key`. If the hash conflicts, we will simply
        // return an error. See the TODO below.
        const { assert!(SHMMNI <= u32::MAX as usize) };
        let shmid = IpcId::new(key.cast_unsigned() % SHMMNI as u32 + 1);

        loop {
            match self.shm_ids.with(shmid, |shm_set| {
                if shm_set.permission().key() != key {
                    if flags.contains(IpcFlags::IPC_CREAT) {
                        // TODO: Manage all keys in a data structure (e.g., a map)
                        return_errno_with_message!(Errno::ENOSPC, "key hashes conflict");
                    }
                    return_errno_with_message!(Errno::ENOENT, "the key does not exist");
                }

                Self::validate_shm_set(
                    shm_set,
                    ShmPermissionMode::ALTER | ShmPermissionMode::READ,
                )?;

                if flags.contains(IpcFlags::IPC_CREAT | IpcFlags::IPC_EXCL) {
                    return_errno_with_message!(
                        Errno::EEXIST,
                        "the segment already exists with IPC_EXCL"
                    );
                }

                if shm_set.size() < size {
                    return_errno_with_message!(Errno::EINVAL, "the segment is too small");
                }

                Ok(shmid)
            }) {
                Err(_id_not_exist) if flags.contains(IpcFlags::IPC_CREAT) => {}
                Err(_id_not_exist) => {
                    return_errno_with_message!(Errno::ENOENT, "the key does not exist");
                }
                Ok(result) => return result,
            }

            match self
                .shm_ids
                .insert_at(shmid, |_| ShmSet::new(key, size, mode, pid, &credentials))
            {
                Ok(()) => return Ok(shmid),
                Err(err) if err.error() == Errno::EEXIST => continue,
                Err(err) => return Err(err),
            }
        }
    }

    fn validate_shm_set(_shm_set: &ShmSet, required_perm: ShmPermissionMode) -> Result<()> {
        if !required_perm.is_empty() {
            // TODO: Support permission check
            warn!("Shared memory doesn't support permission check now");
        }

        Ok(())
    }

    /// Creates a new shared memory segment and returns its ID.
    fn create_shm_set(
        &self,
        size: usize,
        mode: u16,
        pid: Pid,
        credentials: Credentials<ReadOp>,
    ) -> Result<IpcId> {
        self.shm_ids
            .insert_auto(|_| ShmSet::new(IPC_PRIVATE, size, mode, pid, &credentials))
    }

    /// Records an attachment of `shmid` at `addr` by the process `pid`.
    pub fn record_shm_attachment(&self, pid: Pid, addr: Vaddr, shmid: IpcId) {
        self.shm_attachments.write().insert((pid, addr), shmid);
    }

    /// Removes and returns the shared memory ID attached at `addr` by `pid`.
    pub fn remove_shm_attachment(&self, pid: Pid, addr: Vaddr) -> Option<IpcId> {
        self.shm_attachments.write().remove(&(pid, addr))
    }
}

impl NsCommonOps for IpcNamespace {
    const TYPE: NsType = NsType::Ipc;

    fn owner_user_ns(&self) -> Option<&Arc<UserNamespace>> {
        Some(&self.owner)
    }

    fn parent(&self) -> Result<&Arc<Self>> {
        return_errno_with_message!(
            Errno::EINVAL,
            "an IPC namespace does not have a parent namespace"
        );
    }

    fn stashed_dentry(&self) -> &StashedDentry {
        &self.stashed_dentry
    }
}
