// SPDX-License-Identifier: MPL-2.0

//! A subsystem for character devices (or char devices for short).

use core::ops::Range;

use device_id::{DeviceId, MajorId};

use crate::{
    device::Device,
    fs::devtmpfs::{self, DevtmpfsNode},
    prelude::*,
};

#[derive(Debug)]
struct DuplicateDevice<D>(D);

struct DeviceRegistrationTable<D, C> {
    devices: BTreeMap<u32, D>,
    node_context: Option<C>,
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
        }
    }

    fn insert(
        &mut self,
        id: u32,
        device: D,
    ) -> core::result::Result<Option<(D, C)>, DuplicateDevice<D>> {
        if self.devices.contains_key(&id) {
            return Err(DuplicateDevice(device));
        }

        let node = self
            .node_context
            .as_ref()
            .map(|context| (device.clone(), context.clone()));
        self.devices.insert(id, device);
        Ok(node)
    }

    fn activate(&mut self, node_context: C) -> Vec<D> {
        debug_assert!(self.node_context.is_none());
        self.node_context = Some(node_context);
        self.devices.values().cloned().collect()
    }

    fn remove_if(&mut self, id: u32, predicate: impl FnOnce(&D) -> bool) -> Option<D> {
        if self.devices.get(&id).is_some_and(predicate) {
            self.devices.remove(&id)
        } else {
            None
        }
    }
}

// The node context carries nothing: the devtmpfs node for a device is derived
// from the device itself. It exists only to tell "registered before devtmpfs
// was ready" (no node yet) apart from "registered later" (create the node now).
static DEVICE_REGISTRY: Mutex<DeviceRegistrationTable<Arc<dyn Device>, ()>> =
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

    if let Some((device, ())) = node
        && let Err(error) = add_device_node(&device)
        // A device that also goes through `aster_device` has had its node
        // created by that path already, which is not a registration failure.
        && error.error() != Errno::EEXIST
    {
        let _removed = {
            let mut registry = DEVICE_REGISTRY.lock();
            registry.remove_if(id, |registered| Arc::ptr_eq(registered, &device))
        };
        return Err(error);
    }

    Ok(())
}

/// Unregisters an existing char device, returning the device if found.
pub fn unregister(id: DeviceId) -> Result<Arc<dyn Device>> {
    let device = DEVICE_REGISTRY
        .lock()
        .devices
        .remove(&id.to_raw())
        .ok_or_else(|| Error::with_message(Errno::ENOENT, "the char device does not exist"))?;

    if let Some(meta) = device.devtmpfs_meta()
        && let Err(error) = devtmpfs::delete_node(DevtmpfsNode::new(
            device.type_(),
            device.id(),
            meta,
        ))
    {
        warn!(
            "failed to delete devtmpfs node for char device {:?}: {:?}",
            id, error
        );
    }

    Ok(device)
}

/// Looks up a char device of a given device ID.
pub(super) fn lookup(id: DeviceId) -> Option<Arc<dyn Device>> {
    DEVICE_REGISTRY.lock().devices.get(&id.to_raw()).cloned()
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

pub(super) fn init_in_first_process() -> Result<()> {
    let devices = DEVICE_REGISTRY.lock().activate(());
    for device in devices {
        // Devices that also go through `aster_device` (the memory devices, the
        // console) have had their nodes created by that path already.
        if let Err(error) = add_device_node(&device)
            && error.error() != Errno::EEXIST
        {
            return Err(error);
        }
    }

    Ok(())
}

fn add_device_node(device: &Arc<dyn Device>) -> Result<()> {
    let Some(devtmpfs_meta) = device.devtmpfs_meta() else {
        return Ok(());
    };
    devtmpfs::create_node(DevtmpfsNode::new(
        device.type_(),
        device.id(),
        devtmpfs_meta,
    ))
}

#[cfg(ktest)]
mod tests {
    use alloc::vec;

    use ostd::prelude::ktest;

    use super::DeviceRegistrationTable;

    #[ktest]
    fn registrations_request_nodes_across_devtmpfs_activation() {
        let mut registrations = DeviceRegistrationTable::new();

        assert_eq!(registrations.insert(1, "early").unwrap(), None);
        assert_eq!(registrations.activate("resolver"), vec!["early"]);
        assert_eq!(
            registrations.insert(2, "late").unwrap(),
            Some(("late", "resolver"))
        );
    }

    #[ktest]
    fn failed_late_registration_can_be_rolled_back() {
        let mut registrations = DeviceRegistrationTable::new();
        registrations.activate("resolver");
        let (device, _) = registrations.insert(1, "failed").unwrap().unwrap();

        assert_eq!(
            registrations.remove_if(1, |registered| *registered == "replacement"),
            None
        );
        assert!(registrations.insert(1, "retry").is_err());
        assert_eq!(
            registrations.remove_if(1, |registered| *registered == device),
            Some("failed")
        );
        assert_eq!(
            registrations.insert(1, "retry").unwrap(),
            Some(("retry", "resolver"))
        );
    }
}
