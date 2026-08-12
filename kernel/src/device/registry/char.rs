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

    if let Some((device, path_resolver)) = node
        && let Err(error) = add_device_node(&device, &path_resolver)
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
    DEVICE_REGISTRY
        .lock()
        .devices
        .remove(&id.to_raw())
        .ok_or_else(|| Error::with_message(Errno::ENOENT, "the char device does not exist"))
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

pub(super) fn init_in_first_process(path_resolver: &PathResolver) -> Result<()> {
    let devices = DEVICE_REGISTRY.lock().activate(path_resolver.clone());
    for device in devices {
        add_device_node(&device, path_resolver)?;
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
