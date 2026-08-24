// SPDX-License-Identifier: MPL-2.0

//! A subsystem for character devices (or char devices for short).

use core::ops::Range;

use device_id::{DeviceId, MajorId};

use crate::{
    device::{Device, DeviceType, add_node},
    fs::vfs::path::PathResolver,
    prelude::*,
};

#[derive(Debug)]
struct DuplicateDevice<D>(D);

#[derive(Clone, Copy)]
struct NodeRequestToken {
    id: u32,
    generation: u64,
}

struct DeviceNodeRequest<D, C> {
    token: NodeRequestToken,
    device: D,
    context: C,
}

enum PendingNodeRollback {
    Remove,
    Restore,
}

enum NodeFinalization {
    Commit,
    Rollback,
}

enum DeviceRegistration<D> {
    Ready(D),
    PendingNode {
        device: D,
        generation: u64,
        rollback: PendingNodeRollback,
    },
}

struct DeviceRegistrationTable<D, C> {
    devices: BTreeMap<u32, DeviceRegistration<D>>,
    node_context: Option<C>,
    next_generation: u64,
}

impl<D, C> DeviceRegistrationTable<D, C>
where
    D: Clone,
    C: Clone,
{
    const fn new() -> Self {
        Self {
            devices: BTreeMap::new(),
            node_context: None,
            next_generation: 0,
        }
    }

    fn insert(
        &mut self,
        id: u32,
        device: D,
    ) -> core::result::Result<Option<DeviceNodeRequest<D, C>>, DuplicateDevice<D>> {
        if self.devices.contains_key(&id) {
            return Err(DuplicateDevice(device));
        }

        let Some(context) = self.node_context.as_ref().cloned() else {
            self.devices.insert(id, DeviceRegistration::Ready(device));
            return Ok(None);
        };

        let token = self.new_token(id);
        let request = DeviceNodeRequest {
            token,
            device: device.clone(),
            context,
        };
        self.devices.insert(
            id,
            DeviceRegistration::PendingNode {
                device,
                generation: token.generation,
                rollback: PendingNodeRollback::Remove,
            },
        );
        Ok(Some(request))
    }

    fn activate(&mut self, node_context: C) -> Vec<DeviceNodeRequest<D, C>> {
        debug_assert!(self.node_context.is_none());
        self.node_context = Some(node_context);

        let ids: Vec<_> = self.devices.keys().copied().collect();
        let mut requests = Vec::with_capacity(ids.len());
        for id in ids {
            let token = self.new_token(id);
            let DeviceRegistration::Ready(device) = self.devices.remove(&id).unwrap() else {
                unreachable!("only ready devices can exist before activation");
            };
            requests.push(DeviceNodeRequest {
                token,
                device: device.clone(),
                context: self.node_context.as_ref().unwrap().clone(),
            });
            self.devices.insert(
                id,
                DeviceRegistration::PendingNode {
                    device,
                    generation: token.generation,
                    rollback: PendingNodeRollback::Restore,
                },
            );
        }
        requests
    }

    fn lookup(&self, id: u32) -> Option<D> {
        match self.devices.get(&id) {
            Some(
                DeviceRegistration::Ready(device)
                | DeviceRegistration::PendingNode {
                    device,
                    rollback: PendingNodeRollback::Restore,
                    ..
                },
            ) => Some(device.clone()),
            _ => None,
        }
    }

    fn remove(&mut self, id: u32) -> Option<D> {
        if !matches!(self.devices.get(&id), Some(DeviceRegistration::Ready(_))) {
            return None;
        }

        let Some(DeviceRegistration::Ready(device)) = self.devices.remove(&id) else {
            unreachable!();
        };
        Some(device)
    }

    fn commit_node(&mut self, token: NodeRequestToken) -> bool {
        self.finalize_node(token, NodeFinalization::Commit)
    }

    fn rollback_node(&mut self, token: NodeRequestToken) -> bool {
        self.finalize_node(token, NodeFinalization::Rollback)
    }

    fn new_token(&mut self, id: u32) -> NodeRequestToken {
        self.next_generation = self
            .next_generation
            .checked_add(1)
            .expect("device node request generation exhausted");
        NodeRequestToken {
            id,
            generation: self.next_generation,
        }
    }

    fn finalize_node(&mut self, token: NodeRequestToken, finalization: NodeFinalization) -> bool {
        let is_matching = matches!(
            self.devices.get(&token.id),
            Some(DeviceRegistration::PendingNode { generation, .. })
                if *generation == token.generation
        );
        if !is_matching {
            return false;
        }

        let Some(DeviceRegistration::PendingNode {
            device, rollback, ..
        }) = self.devices.remove(&token.id)
        else {
            unreachable!();
        };
        if matches!(finalization, NodeFinalization::Commit)
            || matches!(rollback, PendingNodeRollback::Restore)
        {
            self.devices
                .insert(token.id, DeviceRegistration::Ready(device));
        }
        true
    }
}

static DEVICE_REGISTRY: Mutex<DeviceRegistrationTable<Arc<dyn Device>, PathResolver>> =
    Mutex::new(DeviceRegistrationTable::new());

/// Registers a new char device.
pub fn register(device: Arc<dyn Device>) -> Result<()> {
    let id = device.id().to_raw();
    let insertion = {
        let mut registry = DEVICE_REGISTRY.lock();
        registry.insert(id, device)
    };
    let node = insertion
        .map_err(|_| Error::with_message(Errno::EEXIST, "the char device already exists"))?;

    if let Some(request) = node {
        if let Err(error) = add_device_node(&request.device, &request.context) {
            let rolled_back = DEVICE_REGISTRY.lock().rollback_node(request.token);
            debug_assert!(rolled_back);
            return Err(error);
        }
        let committed = DEVICE_REGISTRY.lock().commit_node(request.token);
        debug_assert!(committed);
    }

    Ok(())
}

/// Unregisters an existing char device, returning the device if found.
pub fn unregister(id: DeviceId) -> Result<Arc<dyn Device>> {
    DEVICE_REGISTRY
        .lock()
        .remove(id.to_raw())
        .ok_or_else(|| Error::with_message(Errno::ENOENT, "the char device does not exist"))
}

/// Looks up a char device of a given device ID.
pub(super) fn lookup(id: DeviceId) -> Option<Arc<dyn Device>> {
    DEVICE_REGISTRY.lock().lookup(id.to_raw())
}

/// The maximum value of the major device ID of a char device.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.13/source/fs/char_dev.c#L104>.
pub const MAX_MAJOR: u16 = 511;

/// The ranges of free char majors.
///
/// Reference: <https://elixir.bootlin.com/linux/v6.13/source/include/linux/fs.h#L2840>.
const DYNAMIC_MAJOR_ID_RANGES: [Range<u16>; 2] = [234..255, 384..512];

static MAJORS: Mutex<BTreeSet<u16>> = Mutex::new(BTreeSet::new());

/// Acquires a major ID.
///
/// The returned `MajorIdOwner` object represents the ownership to the major ID.
/// Until the object is dropped, this major ID cannot be acquired via `acquire_major` or `allocate_major` again.
pub fn acquire_major(major: MajorId) -> Result<MajorIdOwner> {
    if major.get() > MAX_MAJOR {
        return_errno_with_message!(Errno::EINVAL, "the major ID is invalid");
    }

    if MAJORS.lock().insert(major.get()) {
        Ok(MajorIdOwner(major))
    } else {
        return_errno_with_message!(Errno::EEXIST, "the major ID has already been acquired")
    }
}

/// Allocates a major ID.
///
/// The returned `MajorIdOwner` object represents the ownership to the major ID.
/// Until the object is dropped, this major ID cannot be acquired via `acquire_major` or `allocate_major` again.
#[expect(dead_code)]
pub fn allocate_major() -> Result<MajorIdOwner> {
    let mut majors = MAJORS.lock();

    for id in DYNAMIC_MAJOR_ID_RANGES
        .iter()
        .flat_map(|range| range.clone().rev())
    {
        if majors.insert(id) {
            return Ok(MajorIdOwner(MajorId::new(id)));
        }
    }

    return_errno_with_message!(Errno::ENOSPC, "no more major IDs are available");
}

/// An owned major ID.
///
/// Each instances of this type will unregister the major ID when dropped.
pub struct MajorIdOwner(MajorId);

impl MajorIdOwner {
    /// Returns the major ID.
    pub fn get(&self) -> MajorId {
        self.0
    }
}

impl Drop for MajorIdOwner {
    fn drop(&mut self) {
        MAJORS.lock().remove(&self.0.get());
    }
}

pub(super) fn init_in_first_process(path_resolver: &PathResolver) -> Result<()> {
    let requests = DEVICE_REGISTRY.lock().activate(path_resolver.clone());
    let mut requests = requests.into_iter();
    while let Some(request) = requests.next() {
        if let Err(error) = add_device_node(&request.device, &request.context) {
            let mut registry = DEVICE_REGISTRY.lock();
            let rolled_back = registry.rollback_node(request.token);
            debug_assert!(rolled_back);
            for unprocessed in requests {
                let rolled_back = registry.rollback_node(unprocessed.token);
                debug_assert!(rolled_back);
            }
            return Err(error);
        }

        let committed = DEVICE_REGISTRY.lock().commit_node(request.token);
        debug_assert!(committed);
    }

    Ok(())
}

fn add_device_node(device: &Arc<dyn Device>, path_resolver: &PathResolver) -> Result<()> {
    let Some(devtmpfs_meta) = device.devtmpfs_meta() else {
        return Ok(());
    };
    let dev_id = device.id().as_encoded_u64();
    add_node(DeviceType::Char, dev_id, &devtmpfs_meta, path_resolver)?;
    Ok(())
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::DeviceRegistrationTable;

    #[ktest]
    fn late_registration_is_hidden_until_node_creation_commits() {
        let mut registrations = DeviceRegistrationTable::new();
        assert!(registrations.activate("resolver").is_empty());

        let request = registrations.insert(1, "late").unwrap().unwrap();
        assert_eq!(request.device, "late");
        assert_eq!(request.context, "resolver");
        assert_eq!(registrations.lookup(1), None);
        assert_eq!(registrations.remove(1), None);
        assert!(registrations.insert(1, "duplicate").is_err());

        assert!(registrations.commit_node(request.token));
        assert_eq!(registrations.lookup(1), Some("late"));
        assert_eq!(registrations.remove(1), Some("late"));
    }

    #[ktest]
    fn failed_late_node_creation_rolls_back_matching_registration() {
        let mut registrations = DeviceRegistrationTable::new();
        registrations.activate("resolver");

        let failed = registrations.insert(1, "failed").unwrap().unwrap();
        assert!(registrations.rollback_node(failed.token));
        let retry = registrations.insert(1, "retry").unwrap().unwrap();
        assert!(registrations.commit_node(retry.token));
        assert_eq!(registrations.lookup(1), Some("retry"));
    }

    #[ktest]
    fn stale_node_token_cannot_finalize_a_new_registration() {
        let mut registrations = DeviceRegistrationTable::new();
        registrations.activate("resolver");

        let first = registrations.insert(1, "first").unwrap().unwrap();
        assert!(registrations.rollback_node(first.token));
        let second = registrations.insert(1, "second").unwrap().unwrap();

        assert!(!registrations.commit_node(first.token));
        assert!(!registrations.rollback_node(first.token));
        assert_eq!(registrations.lookup(1), None);
        assert_eq!(registrations.remove(1), None);
        assert!(registrations.insert(1, "duplicate").is_err());
        assert!(registrations.commit_node(second.token));
        assert_eq!(registrations.lookup(1), Some("second"));
    }

    #[ktest]
    fn activation_pending_entries_commit_or_restore_to_ready() {
        let mut registrations = DeviceRegistrationTable::new();
        registrations.insert(1, "success").unwrap();
        registrations.insert(2, "failure").unwrap();
        registrations.insert(3, "unprocessed").unwrap();

        let requests = registrations.activate("resolver");
        assert_eq!(requests.len(), 3);
        assert_eq!(registrations.lookup(1), Some("success"));
        assert_eq!(registrations.lookup(2), Some("failure"));
        assert_eq!(registrations.lookup(3), Some("unprocessed"));
        assert_eq!(registrations.remove(1), None);
        assert_eq!(registrations.remove(2), None);
        assert_eq!(registrations.remove(3), None);
        assert!(registrations.insert(1, "replacement").is_err());
        assert!(registrations.insert(2, "replacement").is_err());
        assert!(registrations.insert(3, "replacement").is_err());

        assert!(registrations.commit_node(requests[0].token));
        assert!(registrations.rollback_node(requests[1].token));
        assert!(registrations.rollback_node(requests[2].token));

        assert_eq!(registrations.lookup(1), Some("success"));
        assert_eq!(registrations.lookup(2), Some("failure"));
        assert_eq!(registrations.lookup(3), Some("unprocessed"));
    }
}
