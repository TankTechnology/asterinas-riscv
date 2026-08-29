// SPDX-License-Identifier: MPL-2.0

//! PID namespaces.
//!
//! A PID namespace gives its member processes an isolated view of process
//! IDs: the first process in a namespace is PID 1 (the namespace init), and
//! every process is also visible — under a different PID — in all ancestor
//! namespaces up to the initial one.
//!
//! The global PID/TID allocator remains the source of truth for
//! kernel-unique IDs; a namespace only assigns *virtual* PIDs that are
//! translated at the system call and procfs boundaries.

use spin::Once;

use super::super::{Process, UserNamespace};
use crate::{
    fs::pseudofs::{NsCommonOps, NsType, StashedDentry},
    prelude::*,
};

/// A PID namespace.
pub struct PidNamespace {
    /// The parent namespace; `None` only for the initial PID namespace.
    parent: Option<Arc<PidNamespace>>,
    /// The user namespace that owns this PID namespace (the user namespace
    /// active when it was created).
    owner: Arc<UserNamespace>,
    stashed_dentry: StashedDentry,
    inner: Mutex<PidNsInner>,
}

struct PidNsInner {
    /// The next virtual PID to allocate in this namespace.
    ///
    /// Virtual PID 1 belongs to the namespace init process.
    next_vpid: u32,
    /// The processes visible in this namespace, indexed by virtual PID.
    vpids: BTreeMap<u32, Weak<Process>>,
}

impl PidNamespace {
    /// Returns a reference to the singleton initial PID namespace.
    pub fn get_init_singleton() -> &'static Arc<PidNamespace> {
        static INIT: Once<Arc<PidNamespace>> = Once::new();

        INIT.call_once(|| {
            Arc::new(Self {
                parent: None,
                owner: UserNamespace::get_init_singleton().clone(),
                stashed_dentry: StashedDentry::new(),
                inner: Mutex::new(PidNsInner {
                    // Virtual PIDs in the initial namespace are the global
                    // PIDs; the allocator here only serves as a fallback for
                    // defensive registration and is never actually used.
                    next_vpid: 1,
                    vpids: BTreeMap::new(),
                }),
            })
        })
    }

    /// Creates a child PID namespace owned by `owner`.
    pub fn new_child(self: &Arc<Self>, owner: Arc<UserNamespace>) -> Arc<Self> {
        Arc::new(Self {
            parent: Some(self.clone()),
            owner,
            stashed_dentry: StashedDentry::new(),
            inner: Mutex::new(PidNsInner {
                next_vpid: 1,
                vpids: BTreeMap::new(),
            }),
        })
    }

    /// Returns whether this namespace is the same as, or a descendant of,
    /// the given namespace.
    pub fn is_same_or_descendant_of(self: &Arc<Self>, ancestor: &Arc<Self>) -> bool {
        let mut current = self;
        loop {
            if Arc::ptr_eq(current, ancestor) {
                return true;
            }
            let Some(parent) = current.parent_ns() else {
                return false;
            };
            current = parent;
        }
    }

    /// Returns whether this is the initial PID namespace.
    pub fn is_init(&self) -> bool {
        self.parent.is_none()
    }

    /// Returns the parent namespace, if any.
    pub fn parent_ns(&self) -> Option<&Arc<PidNamespace>> {
        self.parent.as_ref()
    }

    /// Registers `process` in this namespace and all of its ancestors,
    /// allocating a virtual PID in each non-initial namespace.
    ///
    /// In the initial namespace the virtual PID is the process's global PID.
    ///
    /// Returns the `(namespace, virtual PID)` pairs from the innermost
    /// namespace outwards.
    pub fn register_process(
        self: &Arc<Self>,
        process: &Weak<Process>,
        global_pid: u32,
    ) -> Vec<(Arc<PidNamespace>, u32)> {
        let mut result = Vec::new();
        let mut current = self;
        loop {
            let vpid = if current.is_init() {
                global_pid
            } else {
                let mut inner = current.inner.lock();
                let vpid = inner.next_vpid;
                inner.next_vpid += 1;
                inner.vpids.insert(vpid, process.clone());
                vpid
            };
            result.push((current.clone(), vpid));

            let Some(parent) = current.parent_ns() else {
                break;
            };
            current = parent;
        }
        result
    }

    /// Removes the process with the given virtual PID from this namespace.
    pub fn remove_vpid(&self, vpid: u32) {
        if self.is_init() {
            return;
        }
        self.inner.lock().vpids.remove(&vpid);
    }

    /// Returns the process with the given virtual PID in this namespace.
    pub fn process_of_vpid(&self, vpid: u32) -> Option<Arc<Process>> {
        if self.is_init() {
            return super::super::pid_table::pid_table_mut().get_process(vpid as _);
        }
        self.inner.lock().vpids.get(&vpid).and_then(Weak::upgrade)
    }

    /// Returns the virtual PIDs and processes currently registered in this
    /// namespace (not including ancestors).
    pub fn vpids_snapshot(&self) -> Vec<(u32, Arc<Process>)> {
        self.inner
            .lock()
            .vpids
            .iter()
            .filter_map(|(vpid, weak)| weak.upgrade().map(|process| (*vpid, process)))
            .collect()
    }
}

impl NsCommonOps for PidNamespace {
    const TYPE: NsType = NsType::Pid;

    fn owner_user_ns(&self) -> Option<&Arc<UserNamespace>> {
        Some(&self.owner)
    }

    fn parent(&self) -> Result<&Arc<Self>> {
        // The initial PID namespace has no parent; `NS_GET_PARENT` on it
        // fails with `EPERM`, as in Linux.
        self.parent.as_ref().ok_or_else(|| {
            Error::with_message(Errno::EPERM, "the initial PID namespace has no parent")
        })
    }

    fn stashed_dentry(&self) -> &StashedDentry {
        &self.stashed_dentry
    }
}
