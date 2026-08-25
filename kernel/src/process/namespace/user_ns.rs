// SPDX-License-Identifier: MPL-2.0

use spin::Once;

use crate::{
    fs::pseudofs::{NsCommonOps, NsType, StashedDentry},
    prelude::*,
    process::{Gid, Uid},
};

/// A single ID mapping extent, in the same shape as a line of
/// `/proc/[pid]/uid_map`: `count` IDs starting at `first` inside this
/// namespace map to IDs starting at `lower_first` in the parent namespace.
///
/// `lower_first` is stored as a global kernel ID (kuid/kgid); translation
/// from the parent-namespace view happens when the map is written.
#[derive(Clone, Copy, Debug)]
pub struct IdMapExtent {
    pub first: u32,
    pub lower_first: u32,
    pub count: u32,
}

/// An ID map attached to a user namespace.
///
/// A map can be written only once (cgroup-v2-style "write once" rule),
/// matching Linux's behavior for `/proc/[pid]/{uid,gid}_map`.
pub struct IdMap {
    extents: Vec<IdMapExtent>,
    written: bool,
}

impl IdMap {
    /// The identity map of the initial user namespace.
    fn identity() -> Self {
        Self {
            extents: vec![IdMapExtent {
                first: 0,
                lower_first: 0,
                count: u32::MAX,
            }],
            written: true,
        }
    }

    /// The empty map of a freshly created user namespace.
    fn empty() -> Self {
        Self {
            extents: Vec::new(),
            written: false,
        }
    }

    /// Whether the map has been written already.
    pub fn is_written(&self) -> bool {
        self.written
    }

    /// Writes the map. Fails with `EPERM` if the map was written before.
    pub fn write(&mut self, extents: Vec<IdMapExtent>) -> Result<()> {
        if self.written {
            return_errno_with_message!(Errno::EPERM, "the ID map has already been written");
        }
        self.extents = extents;
        self.written = true;
        Ok(())
    }

    /// Returns the written extents.
    pub fn extents(&self) -> &[IdMapExtent] {
        &self.extents
    }

    /// Maps a global kernel ID to the ID visible inside this namespace.
    ///
    /// Returns `None` if the ID is not mapped.
    pub fn map_down(&self, kuid: u32) -> Option<u32> {
        for extent in &self.extents {
            let Some(delta) = kuid.checked_sub(extent.lower_first) else {
                continue;
            };
            if delta < extent.count {
                return extent.first.checked_add(delta);
            }
        }
        None
    }

    /// Maps an ID visible inside this namespace back to the global kernel ID.
    ///
    /// Returns `None` if the ID is not mapped.
    pub fn map_up(&self, uid: u32) -> Option<u32> {
        for extent in &self.extents {
            if uid >= extent.first && uid - extent.first < extent.count {
                return Some(extent.lower_first + (uid - extent.first));
            }
        }
        None
    }
}

/// The user namespace.
pub struct UserNamespace {
    stashed_dentry: StashedDentry,
    /// The parent namespace; `None` only for the initial user namespace.
    parent: Option<Arc<UserNamespace>>,
    /// The effective UID (in the parent namespace) of the creating process.
    owner_uid: Uid,
    uid_map: Mutex<IdMap>,
    gid_map: Mutex<IdMap>,
    /// Whether `setgroups` has been disabled via `/proc/[pid]/setgroups`.
    ///
    /// `None` until the file is written; see Linux's `setgroups` handling in
    /// `kernel/user_namespace.c`.
    setgroups_denied: Mutex<bool>,
}

impl UserNamespace {
    /// Returns a reference to the singleton initial user namespace.
    pub fn get_init_singleton() -> &'static Arc<UserNamespace> {
        static INIT: Once<Arc<UserNamespace>> = Once::new();

        INIT.call_once(|| {
            Arc::new(Self {
                stashed_dentry: StashedDentry::new(),
                parent: None,
                owner_uid: Uid::new_root(),
                uid_map: Mutex::new(IdMap::identity()),
                gid_map: Mutex::new(IdMap::identity()),
                setgroups_denied: Mutex::new(false),
            })
        })
    }

    /// Creates a child user namespace owned by `owner` (the creator's
    /// effective UID).
    pub fn new_child(self: &Arc<Self>, owner: Uid) -> Arc<Self> {
        Arc::new(Self {
            stashed_dentry: StashedDentry::new(),
            parent: Some(self.clone()),
            owner_uid: owner,
            uid_map: Mutex::new(IdMap::empty()),
            gid_map: Mutex::new(IdMap::empty()),
            setgroups_denied: Mutex::new(false),
        })
    }

    /// Returns the owner UID of the user namespace (a global kernel ID).
    pub fn owner_uid(&self) -> Result<Uid> {
        Ok(self.owner_uid)
    }

    /// Returns the parent user namespace, if any.
    pub fn parent_ns(&self) -> Option<&Arc<UserNamespace>> {
        self.parent.as_ref()
    }

    /// Returns whether this namespace is the same as, or an ancestor of, the other namespace.
    pub fn is_same_or_ancestor_of(self: &Arc<Self>, other: &Arc<Self>) -> bool {
        let mut current = other;
        loop {
            if Arc::ptr_eq(self, current) {
                return true;
            }
            let Some(parent) = current.parent_ns() else {
                return false;
            };
            current = parent;
        }
    }

    /// Maps a global kernel UID to the UID visible inside this namespace.
    ///
    /// Unmapped IDs become the overflow UID (65534), matching Linux's
    /// `from_kuid` behavior.
    pub fn map_kuid(&self, kuid: Uid) -> Uid {
        self.uid_map
            .lock()
            .map_down(u32::from(kuid))
            .map(Uid::new)
            .unwrap_or(Uid::OVERFLOW)
    }

    /// Maps a global kernel GID to the GID visible inside this namespace.
    pub fn map_kgid(&self, kgid: Gid) -> Gid {
        self.gid_map
            .lock()
            .map_down(u32::from(kgid))
            .map(Gid::new)
            .unwrap_or(Gid::OVERFLOW)
    }

    /// Maps a UID visible inside this namespace to a global kernel UID.
    ///
    /// Unmapped IDs fail, matching Linux's `make_kuid` behavior in the
    /// `setuid`-family system calls.
    pub fn make_kuid(&self, uid: u32) -> Result<Uid> {
        self.uid_map
            .lock()
            .map_up(uid)
            .map(Uid::new)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "UID is not mapped in the user namespace"))
    }

    /// Maps a GID visible inside this namespace to a global kernel GID.
    pub fn make_kgid(&self, gid: u32) -> Result<Gid> {
        self.gid_map
            .lock()
            .map_up(gid)
            .map(Gid::new)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "GID is not mapped in the user namespace"))
    }

    /// Locks the UID map of this namespace.
    pub fn lock_uid_map(&self) -> MutexGuard<'_, IdMap> {
        self.uid_map.lock()
    }

    /// Locks the GID map of this namespace.
    pub fn lock_gid_map(&self) -> MutexGuard<'_, IdMap> {
        self.gid_map.lock()
    }

    /// Returns whether `setgroups` has been denied in this namespace.
    pub fn is_setgroups_denied(&self) -> bool {
        *self.setgroups_denied.lock()
    }

    /// Denies `setgroups` in this namespace.
    ///
    /// Fails with `EPERM` if the GID map has already been written, matching
    /// Linux (setgroups can only be disabled before the GID map is set).
    pub fn deny_setgroups(&self) -> Result<()> {
        if self.gid_map.lock().is_written() {
            return_errno_with_message!(
                Errno::EPERM,
                "setgroups cannot be disabled after the GID map is written"
            );
        }
        *self.setgroups_denied.lock() = true;
        Ok(())
    }
}

impl NsCommonOps for UserNamespace {
    const TYPE: NsType = NsType::User;

    fn owner_user_ns(&self) -> Option<&Arc<UserNamespace>> {
        // For user namespaces, `NS_GET_USERNS` returns the parent user namespace
        // rather than an "owner". The initial user namespace has no parent.
        // Reference: <https://elixir.bootlin.com/linux/v6.19/source/kernel/user_namespace.c#L1406>
        self.parent.as_ref()
    }

    fn parent(&self) -> Result<&Arc<Self>> {
        // User namespaces do not support `NS_GET_PARENT`.
        // Reference: <https://elixir.bootlin.com/linux/v6.19/source/kernel/user_namespace.c#L1407>
        return_errno_with_message!(Errno::EPERM, "user namespaces do not support NS_GET_PARENT");
    }

    fn stashed_dentry(&self) -> &StashedDentry {
        &self.stashed_dentry
    }
}
