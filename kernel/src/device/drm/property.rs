// SPDX-License-Identifier: MPL-2.0

//! DRM property system for atomic modesetting.
//!
//! Manages global property definitions, per-object property values, and property blob storage.
//! Properties are defined once at boot and referenced by id across all DRM objects.

use core::sync::atomic::{AtomicU32, Ordering};

use ostd::mm::VmIo;

use super::{
    AtomicKmsObject, CRTC_ID, DRM_MODE_OBJECT_CONNECTOR, DRM_MODE_OBJECT_CRTC,
    DRM_MODE_OBJECT_PLANE, DRM_PLANE_TYPE_PRIMARY, DrmModeCreatePropertyBlob,
    DrmModeDestroyPropertyBlob, DrmModeGetBlob, DrmModeGetProperty, DrmModeObjGetProperties,
};
use crate::{
    context::current_userspace,
    prelude::*,
    util::ioctl::{InOutData, Ioctl},
};

/// `DRM_MODE_PROP_IMMUTABLE` — the property cannot be changed by userspace.
const DRM_MODE_PROP_IMMUTABLE: u32 = 1 << 2;

/// `DRM_PROP_NAME_LEN` — fixed width of property and enum names in the UAPI.
const DRM_PROP_NAME_LEN: usize = 32;

/// Upper bound for a user-created property blob.
///
/// The current driver consumes only 68-byte mode blobs. Keeping a modest
/// ceiling leaves room for future color-management blobs without allowing one
/// ioctl to request an effectively unbounded kernel allocation.
const MAX_PROPERTY_BLOB_SIZE: usize = 64 * 1024;

/// Internal identity of a property, independent of its UAPI display name.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum PropertyKind {
    Active,
    ModeId,
    ConnectorCrtcId,
    PlaneType,
    PlaneFbId,
    PlaneCrtcId,
    SrcX,
    SrcY,
    SrcW,
    SrcH,
    CrtcX,
    CrtcY,
    CrtcW,
    CrtcH,
}

/// Property type enum matching the DRM UAPI property type constants.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum PropertyType {
    Range = 0,
    Blob = 2,
    Object = 3,
    SignedRange = 4,
    Enum = 5,
}

impl PropertyType {
    /// Maps to the `DRM_MODE_PROP_*` bits of the UAPI `flags` field
    /// (`struct drm_mode_get_property`).
    fn uapi_flags(self) -> u32 {
        const DRM_MODE_PROP_RANGE: u32 = 1 << 1;
        const DRM_MODE_PROP_ENUM: u32 = 1 << 3;
        const DRM_MODE_PROP_BLOB: u32 = 1 << 4;
        const DRM_MODE_PROP_OBJECT: u32 = 1 << 6;
        const DRM_MODE_PROP_SIGNED_RANGE: u32 = 2 << 6;
        match self {
            PropertyType::Range => DRM_MODE_PROP_RANGE,
            PropertyType::Blob => DRM_MODE_PROP_BLOB,
            PropertyType::Object => DRM_MODE_PROP_OBJECT,
            PropertyType::SignedRange => DRM_MODE_PROP_SIGNED_RANGE,
            PropertyType::Enum => DRM_MODE_PROP_ENUM,
        }
    }
}

/// A single DRM property definition.
#[derive(Debug)]
pub(super) struct Property {
    pub id: u32,
    pub kind: PropertyKind,
    pub name: &'static str,
    pub prop_type: PropertyType,
    pub flags: u32,
    pub min: u64,
    pub max: u64,
}

impl Property {
    /// Returns whether userspace is forbidden from changing this property.
    pub(super) fn is_immutable(&self) -> bool {
        self.flags & DRM_MODE_PROP_IMMUTABLE != 0
    }
}

/// A typed value stored for a property on an object.
#[derive(Clone, Debug)]
pub(super) enum PropertyValue {
    Range(u64),
    SignedRange(i64),
    Object(u32),
    Blob(Option<PropertyBlobRef>),
}

/// A property blob (variable-length binary data referenced by id).
#[derive(Debug)]
pub(super) struct PropertyBlob {
    id: u32,
    data: Vec<u8>,
}

/// A live reference to a property blob.
///
/// The blob remains discoverable after its creator destroys the userspace
/// handle while a committed property still references it. The last reference
/// removes an ownerless blob from the global id namespace.
#[derive(Debug)]
pub(super) struct PropertyBlobRef {
    blob: Arc<PropertyBlob>,
    store: Weak<BlobStore>,
}

impl PropertyBlobRef {
    pub(super) fn id(&self) -> u32 {
        self.blob.id
    }

    pub(super) fn data(&self) -> &[u8] {
        &self.blob.data
    }
}

impl Clone for PropertyBlobRef {
    fn clone(&self) -> Self {
        Self {
            blob: self.blob.clone(),
            store: self.store.clone(),
        }
    }
}

impl Drop for PropertyBlobRef {
    fn drop(&mut self) {
        let Some(store) = self.store.upgrade() else {
            return;
        };
        let mut blobs = store.blobs.lock();
        let should_remove = blobs.get(&self.blob.id).is_some_and(|entry| {
            entry.owner_file_id.is_none()
                && Arc::ptr_eq(&entry.blob, &self.blob)
                && Arc::strong_count(&entry.blob) == 2
        });
        if should_remove {
            blobs.remove(&self.blob.id);
        }
    }
}

struct BlobEntry {
    blob: Arc<PropertyBlob>,
    owner_file_id: Option<u64>,
}

struct BlobStore {
    blobs: SpinLock<BTreeMap<u32, BlobEntry>>,
    next_blob_id: AtomicU32,
}

impl BlobStore {
    fn new() -> Self {
        Self {
            blobs: SpinLock::new(BTreeMap::new()),
            next_blob_id: AtomicU32::new(1),
        }
    }

    fn alloc_id(&self) -> Result<u32> {
        self.next_blob_id
            .try_update(Ordering::Relaxed, Ordering::Relaxed, |id| id.checked_add(1))
            .map_err(|_| Error::with_message(Errno::ENOSPC, "property blob ids exhausted"))
    }

    fn create(self: &Arc<Self>, data: Vec<u8>, owner_file_id: u64) -> Result<u32> {
        let id = self.alloc_id()?;
        let blob = Arc::new(PropertyBlob { id, data });
        self.blobs.lock().insert(
            id,
            BlobEntry {
                blob,
                owner_file_id: Some(owner_file_id),
            },
        );
        Ok(id)
    }

    fn lookup(self: &Arc<Self>, blob_id: u32) -> Result<PropertyBlobRef> {
        let blob = self
            .blobs
            .lock()
            .get(&blob_id)
            .map(|entry| entry.blob.clone())
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown blob id"))?;
        Ok(PropertyBlobRef {
            blob,
            store: Arc::downgrade(self),
        })
    }

    fn destroy(&self, blob_id: u32, owner_file_id: u64) -> Result<()> {
        let mut blobs = self.blobs.lock();
        let entry = blobs
            .get_mut(&blob_id)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown blob id"))?;
        if entry.owner_file_id != Some(owner_file_id) {
            return_errno_with_message!(Errno::EACCES, "property blob belongs to another DRM file");
        }
        entry.owner_file_id = None;
        if Arc::strong_count(&entry.blob) == 1 {
            blobs.remove(&blob_id);
        }
        Ok(())
    }

    fn release_owner(&self, owner_file_id: u64) {
        self.blobs.lock().retain(|_, entry| {
            if entry.owner_file_id == Some(owner_file_id) {
                entry.owner_file_id = None;
            }
            entry.owner_file_id.is_some() || Arc::strong_count(&entry.blob) > 1
        });
    }
}

/// Global property manager — one instance shared across all DRM opens.
pub(super) struct PropertyManager {
    properties: SpinLock<BTreeMap<u32, Arc<Property>>>,
    next_prop_id: AtomicU32,
    blob_store: Arc<BlobStore>,
    /// Per-object property values: keyed by (object_id, object_type).
    object_props: SpinLock<BTreeMap<(u32, u32), BTreeMap<u32, PropertyValue>>>,
    /// Property ids applicable to each object type
    /// (`DRM_MODE_OBJECT_CRTC` / `CONNECTOR` / `PLANE`).
    crtc_props: Vec<u32>,
    connector_props: Vec<u32>,
    plane_props: Vec<u32>,
}

impl PropertyManager {
    pub(super) fn new() -> Self {
        let mut mgr = Self {
            properties: SpinLock::new(BTreeMap::new()),
            next_prop_id: AtomicU32::new(1),
            blob_store: Arc::new(BlobStore::new()),
            object_props: SpinLock::new(BTreeMap::new()),
            crtc_props: Vec::new(),
            connector_props: Vec::new(),
            plane_props: Vec::new(),
        };
        mgr.define_properties();
        mgr
    }

    fn alloc_prop_id(&self) -> u32 {
        self.next_prop_id.fetch_add(1, Ordering::Relaxed)
    }

    /// Registers a property and records it as applicable to `obj_type`.
    fn define(&mut self, obj_type: u32, prop: Property) {
        let id = prop.id;
        self.properties.lock().insert(id, Arc::new(prop));
        match obj_type {
            DRM_MODE_OBJECT_CRTC => self.crtc_props.push(id),
            DRM_MODE_OBJECT_CONNECTOR => self.connector_props.push(id),
            DRM_MODE_OBJECT_PLANE => self.plane_props.push(id),
            _ => unreachable!(),
        }
    }

    /// Defines all standard CRTC, connector, and plane properties.
    fn define_properties(&mut self) {
        // --- CRTC properties ---
        self.define(
            DRM_MODE_OBJECT_CRTC,
            Property {
                id: self.alloc_prop_id(),
                kind: PropertyKind::Active,
                name: "ACTIVE",
                prop_type: PropertyType::Range,
                flags: 0,
                min: 0,
                max: 1,
            },
        );
        self.define(
            DRM_MODE_OBJECT_CRTC,
            Property {
                id: self.alloc_prop_id(),
                kind: PropertyKind::ModeId,
                name: "MODE_ID",
                prop_type: PropertyType::Blob,
                flags: 0,
                min: 0,
                max: u64::MAX,
            },
        );

        // --- Connector properties ---
        self.define(
            DRM_MODE_OBJECT_CONNECTOR,
            Property {
                id: self.alloc_prop_id(),
                kind: PropertyKind::ConnectorCrtcId,
                name: "CRTC_ID",
                prop_type: PropertyType::Object,
                flags: 0,
                min: 0,
                max: u64::MAX,
            },
        );

        // --- Plane properties ---
        self.define(
            DRM_MODE_OBJECT_PLANE,
            Property {
                id: self.alloc_prop_id(),
                kind: PropertyKind::PlaneType,
                name: "type",
                prop_type: PropertyType::Enum,
                flags: DRM_MODE_PROP_IMMUTABLE,
                min: 0,
                max: u64::MAX,
            },
        );
        self.define(
            DRM_MODE_OBJECT_PLANE,
            Property {
                id: self.alloc_prop_id(),
                kind: PropertyKind::PlaneFbId,
                name: "FB_ID",
                prop_type: PropertyType::Object,
                flags: 0,
                min: 0,
                max: u64::MAX,
            },
        );
        self.define(
            DRM_MODE_OBJECT_PLANE,
            Property {
                id: self.alloc_prop_id(),
                kind: PropertyKind::PlaneCrtcId,
                name: "CRTC_ID",
                prop_type: PropertyType::Object,
                flags: 0,
                min: 0,
                max: u64::MAX,
            },
        );
        for (kind, name, prop_type) in [
            (PropertyKind::SrcX, "SRC_X", PropertyType::Range),
            (PropertyKind::SrcY, "SRC_Y", PropertyType::Range),
            (PropertyKind::SrcW, "SRC_W", PropertyType::Range),
            (PropertyKind::SrcH, "SRC_H", PropertyType::Range),
            (PropertyKind::CrtcX, "CRTC_X", PropertyType::SignedRange),
            (PropertyKind::CrtcY, "CRTC_Y", PropertyType::SignedRange),
            (PropertyKind::CrtcW, "CRTC_W", PropertyType::Range),
            (PropertyKind::CrtcH, "CRTC_H", PropertyType::Range),
        ] {
            let (min, max) = if prop_type == PropertyType::SignedRange {
                (i64::MIN as u64, i64::MAX as u64)
            } else {
                (0, u64::MAX)
            };
            self.define(
                DRM_MODE_OBJECT_PLANE,
                Property {
                    id: self.alloc_prop_id(),
                    kind,
                    name,
                    prop_type,
                    flags: 0,
                    min,
                    max,
                },
            );
        }
    }

    /// Returns the list of property ids applicable to an object type.
    pub(super) fn property_ids_for_object(&self, obj_type: u32) -> &[u32] {
        match obj_type {
            DRM_MODE_OBJECT_CRTC => &self.crtc_props,
            DRM_MODE_OBJECT_CONNECTOR => &self.connector_props,
            DRM_MODE_OBJECT_PLANE => &self.plane_props,
            _ => &[],
        }
    }

    /// Returns whether a property is applicable to an object type.
    pub(super) fn property_applies(&self, obj_type: u32, prop_id: u32) -> bool {
        self.property_ids_for_object(obj_type).contains(&prop_id)
    }

    /// Looks up a property by id.
    pub(super) fn lookup_property(&self, prop_id: u32) -> Result<Arc<Property>> {
        self.properties
            .lock()
            .get(&prop_id)
            .cloned()
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown property id"))
    }

    /// Gets the current value of a property on an object.
    pub(super) fn get_value(
        &self,
        obj_id: u32,
        obj_type: u32,
        prop_id: u32,
    ) -> Option<PropertyValue> {
        self.object_props
            .lock()
            .get(&(obj_id, obj_type))
            .and_then(|m| m.get(&prop_id).cloned())
    }

    /// Sets a property value for an object.
    pub(super) fn set_value(&self, obj_id: u32, obj_type: u32, prop_id: u32, value: PropertyValue) {
        let previous = self
            .object_props
            .lock()
            .entry((obj_id, obj_type))
            .or_default()
            .insert(prop_id, value);
        drop(previous);
    }

    /// Creates a blob owned by one open DRM file.
    pub(super) fn create_blob(&self, data: Vec<u8>, owner_file_id: u64) -> Result<u32> {
        self.blob_store.create(data, owner_file_id)
    }

    /// Destroys a blob owned by one open DRM file.
    pub(super) fn destroy_blob(&self, blob_id: u32, owner_file_id: u64) -> Result<()> {
        self.blob_store.destroy(blob_id, owner_file_id)
    }

    /// Looks up a blob by id and keeps it alive for the returned reference.
    pub(super) fn lookup_blob(&self, blob_id: u32) -> Result<PropertyBlobRef> {
        self.blob_store.lookup(blob_id)
    }

    /// Releases userspace ownership of every blob created by a closing file.
    pub(super) fn release_file_blobs(&self, owner_file_id: u64) {
        self.blob_store.release_owner(owner_file_id);
    }
}

/// `DRM_IOCTL_MODE_CREATEPROPBLOB`: create a property blob.
pub(super) fn create_property_blob(
    handle: &super::DriHandle,
    cmd: Ioctl<b'd', 0xbd, true, InOutData<DrmModeCreatePropertyBlob>>,
) -> Result<i32> {
    let mut req = cmd.read()?;
    let length = req.length as usize;
    if length == 0 || length > MAX_PROPERTY_BLOB_SIZE {
        return_errno_with_message!(Errno::EINVAL, "unsupported property blob length");
    }
    let mut data = Vec::new();
    data.try_reserve_exact(length)
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate property blob"))?;
    data.resize(length, 0);
    current_userspace!().read_bytes(req.data_ptr as usize, &mut data)?;
    let blob_id = handle
        .gpu_manager
        .property_manager
        .create_blob(data, handle.file_id)?;
    req.blob_id = blob_id;
    if let Err(error) = cmd.write(&req) {
        let _ = handle
            .gpu_manager
            .property_manager
            .destroy_blob(blob_id, handle.file_id);
        return Err(error);
    }
    Ok(0)
}

/// `DRM_IOCTL_MODE_DESTROYPROPBLOB`: destroy a property blob.
pub(super) fn destroy_property_blob(
    handle: &super::DriHandle,
    cmd: Ioctl<b'd', 0xbe, true, InOutData<DrmModeDestroyPropertyBlob>>,
) -> Result<i32> {
    let req = cmd.read()?;
    handle
        .gpu_manager
        .property_manager
        .destroy_blob(req.blob_id, handle.file_id)?;
    Ok(0)
}
/// Converts a stored property value to the raw `u64` of the UAPI.
fn value_to_u64(value: &PropertyValue) -> u64 {
    match value {
        PropertyValue::Range(x) => *x,
        PropertyValue::SignedRange(x) => *x as u64,
        PropertyValue::Object(x) => *x as u64,
        PropertyValue::Blob(blob) => blob.as_ref().map_or(0, |blob| u64::from(blob.id())),
    }
}

/// The default value of a property that userspace has not explicitly set.
///
/// The display is active from boot, so `ACTIVE` defaults to 1 and `CRTC_ID`
/// references default to the device's single CRTC.
fn default_value(prop: &Property) -> u64 {
    match prop.kind {
        PropertyKind::Active => 1,
        PropertyKind::ConnectorCrtcId | PropertyKind::PlaneCrtcId => u64::from(CRTC_ID),
        PropertyKind::PlaneType => DRM_PLANE_TYPE_PRIMARY as u64,
        _ => 0,
    }
}

/// Copies a NUL-padded name into a fixed `DRM_PROP_NAME_LEN` field.
fn copy_name(name: &str, out: &mut [u8; DRM_PROP_NAME_LEN]) {
    let len = name.len().min(DRM_PROP_NAME_LEN - 1);
    out[..len].copy_from_slice(&name.as_bytes()[..len]);
}

fn user_array_slot(base: u64, index: usize, element_size: usize) -> Result<usize> {
    let base = usize::try_from(base)
        .map_err(|_| Error::with_message(Errno::EFAULT, "userspace pointer overflows"))?;
    index
        .checked_mul(element_size)
        .and_then(|offset| base.checked_add(offset))
        .ok_or_else(|| Error::with_message(Errno::EFAULT, "userspace array pointer overflows"))
}

/// `DRM_IOCTL_MODE_OBJ_GETPROPERTIES`: list an object's properties and values.
pub(super) fn get_obj_properties(
    handle: &super::DriHandle,
    cmd: Ioctl<b'd', 0xb9, true, InOutData<DrmModeObjGetProperties>>,
) -> Result<i32> {
    let mut req = cmd.read()?;
    let prop_mgr = &handle.gpu_manager.property_manager;

    let object = AtomicKmsObject::from_id(req.obj_id)
        .filter(|object| object.object_type() == req.obj_type)
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown DRM object id or type"))?;
    let prop_ids = prop_mgr.property_ids_for_object(object.object_type());

    let total = prop_ids.len() as u32;
    if req.props_ptr != 0 && req.prop_values_ptr != 0 {
        let count = (req.count_props as usize).min(prop_ids.len());
        for (i, prop_id) in prop_ids[..count].iter().enumerate() {
            let prop = prop_mgr.lookup_property(*prop_id)?;
            let value = prop_mgr
                .get_value(req.obj_id, req.obj_type, *prop_id)
                .map(|v| value_to_u64(&v))
                .unwrap_or_else(|| default_value(&prop));
            current_userspace!().write_val(
                user_array_slot(req.props_ptr, i, size_of::<u32>())?,
                prop_id,
            )?;
            current_userspace!().write_val(
                user_array_slot(req.prop_values_ptr, i, size_of::<u64>())?,
                &value,
            )?;
        }
    }
    req.count_props = total;
    cmd.write(&req)?;
    Ok(0)
}

/// `DRM_IOCTL_MODE_GETPROPERTY`: describe a property (name, flags, range).
pub(super) fn get_property(
    handle: &super::DriHandle,
    cmd: Ioctl<b'd', 0xaa, true, InOutData<DrmModeGetProperty>>,
) -> Result<i32> {
    let mut req = cmd.read()?;
    let prop_mgr = &handle.gpu_manager.property_manager;
    let prop = prop_mgr.lookup_property(req.prop_id)?;

    let value_capacity = req.count_values as usize;
    let enum_capacity = req.count_enum_blobs as usize;

    req.flags = prop.prop_type.uapi_flags() | prop.flags;
    copy_name(prop.name, &mut req.name);
    req.count_values = 0;
    req.count_enum_blobs = 0;

    match prop.prop_type {
        PropertyType::Range | PropertyType::SignedRange => {
            req.count_values = 2;
            if req.values_ptr != 0 {
                for (index, value) in [prop.min, prop.max].iter().take(value_capacity).enumerate() {
                    current_userspace!().write_val(
                        user_array_slot(req.values_ptr, index, size_of::<u64>())?,
                        value,
                    )?;
                }
            }
        }
        PropertyType::Enum => {
            // The only enum property is the plane "type".
            const PLANE_TYPES: [(u64, &str); 3] = [
                (0, "Overlay"),
                (DRM_PLANE_TYPE_PRIMARY as u64, "Primary"),
                (2, "Cursor"),
            ];
            req.count_enum_blobs = PLANE_TYPES.len() as u32;
            if req.enum_blob_ptr != 0 {
                for (i, (value, name)) in PLANE_TYPES.iter().take(enum_capacity).enumerate() {
                    let mut entry = super::DrmModePropertyEnum {
                        value: *value,
                        name: [0; DRM_PROP_NAME_LEN],
                    };
                    copy_name(name, &mut entry.name);
                    current_userspace!().write_val(
                        user_array_slot(
                            req.enum_blob_ptr,
                            i,
                            size_of::<super::DrmModePropertyEnum>(),
                        )?,
                        &entry,
                    )?;
                }
            }
        }
        _ => {}
    }

    cmd.write(&req)?;
    Ok(0)
}

/// `DRM_IOCTL_MODE_GETPROPBLOB`: read back a property blob's data.
pub(super) fn get_property_blob(
    handle: &super::DriHandle,
    cmd: Ioctl<b'd', 0xac, true, InOutData<DrmModeGetBlob>>,
) -> Result<i32> {
    let mut req = cmd.read()?;
    let blob = handle
        .gpu_manager
        .property_manager
        .lookup_blob(req.blob_id)?;

    if req.data != 0 && (req.length as usize) >= blob.data().len() {
        current_userspace!().write_bytes(req.data as usize, blob.data())?;
    }
    req.length = blob.data().len() as u32;
    cmd.write(&req)?;
    Ok(0)
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn blob_ownership_and_committed_reference_control_lifetime() {
        let store = Arc::new(BlobStore::new());
        let blob_id = store.create(vec![1, 2, 3], 7).unwrap();
        let committed_reference = store.lookup(blob_id).unwrap();

        assert!(store.destroy(blob_id, 8).is_err());
        store.destroy(blob_id, 7).unwrap();
        assert_eq!(store.lookup(blob_id).unwrap().data(), &[1, 2, 3]);

        drop(committed_reference);
        assert!(store.lookup(blob_id).is_err());
    }
}
