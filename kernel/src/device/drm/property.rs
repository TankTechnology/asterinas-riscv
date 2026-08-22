// SPDX-License-Identifier: MPL-2.0

//! DRM property system for atomic modesetting.
//!
//! Manages global property definitions, per-object property values, and
//! property blob storage. Properties are defined once at boot and referenced
//! by id across all DRM objects.

use core::sync::atomic::{AtomicU32, Ordering};

use ostd::mm::VmIo;

use super::{
    DRM_MODE_OBJECT_CONNECTOR, DRM_MODE_OBJECT_CRTC, DRM_MODE_OBJECT_PLANE, DRM_PLANE_TYPE_PRIMARY,
    DrmModeCreatePropertyBlob, DrmModeDestroyPropertyBlob, DrmModeGetBlob, DrmModeGetProperty,
    DrmModeObjGetProperties,
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

/// Property type enum matching the DRM UAPI property type constants.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
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
    pub name: &'static str,
    pub prop_type: PropertyType,
    pub flags: u32,
    pub min: u64,
    pub max: u64,
}

/// A typed value stored for a property on an object.
#[derive(Clone, Debug)]
pub(super) enum PropertyValue {
    Range(u64),
    SignedRange(i64),
    Object(u32),
    Blob(u32),
    Enum(u32),
}

/// A property blob (variable-length binary data referenced by id).
#[derive(Debug)]
pub(super) struct PropertyBlob {
    pub data: Vec<u8>,
}

/// Global property manager — one instance shared across all DRM opens.
pub(super) struct PropertyManager {
    pub(super) properties: SpinLock<BTreeMap<u32, Arc<Property>>>,
    pub(super) blobs: SpinLock<BTreeMap<u32, Arc<PropertyBlob>>>,
    next_prop_id: AtomicU32,
    next_blob_id: AtomicU32,
    /// Per-object property values: keyed by (object_id, object_type).
    pub(super) object_props: SpinLock<BTreeMap<(u32, u32), BTreeMap<u32, PropertyValue>>>,
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
            blobs: SpinLock::new(BTreeMap::new()),
            next_prop_id: AtomicU32::new(1),
            next_blob_id: AtomicU32::new(1),
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

    fn alloc_blob_id(&self) -> u32 {
        self.next_blob_id.fetch_add(1, Ordering::Relaxed)
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

    /// Define all standard CRTC, connector, and plane properties.
    fn define_properties(&mut self) {
        // --- CRTC properties ---
        self.define(
            DRM_MODE_OBJECT_CRTC,
            Property {
                id: self.alloc_prop_id(),
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
                name: "CRTC_ID",
                prop_type: PropertyType::Object,
                flags: 0,
                min: 0,
                max: u64::MAX,
            },
        );
        for (name, prop_type) in [
            ("SRC_X", PropertyType::Range),
            ("SRC_Y", PropertyType::Range),
            ("SRC_W", PropertyType::Range),
            ("SRC_H", PropertyType::Range),
            ("CRTC_X", PropertyType::SignedRange),
            ("CRTC_Y", PropertyType::SignedRange),
            ("CRTC_W", PropertyType::Range),
            ("CRTC_H", PropertyType::Range),
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

    /// Look up a property by id.
    pub(super) fn lookup_property(&self, prop_id: u32) -> Result<Arc<Property>> {
        self.properties
            .lock()
            .get(&prop_id)
            .cloned()
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown property id"))
    }

    /// Get the current value of a property on an object.
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

    /// Set a property value for an object.
    pub(super) fn set_value(&self, obj_id: u32, obj_type: u32, prop_id: u32, value: PropertyValue) {
        self.object_props
            .lock()
            .entry((obj_id, obj_type))
            .or_default()
            .insert(prop_id, value);
    }

    /// Create a blob from userspace data.
    pub(super) fn create_blob(&self, data: Vec<u8>) -> u32 {
        let id = self.alloc_blob_id();
        self.blobs
            .lock()
            .insert(id, Arc::new(PropertyBlob { data }));
        id
    }

    /// Destroy a blob by id.
    pub(super) fn destroy_blob(&self, blob_id: u32) -> Result<()> {
        self.blobs
            .lock()
            .remove(&blob_id)
            .map(|_| ())
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown blob id"))
    }

    /// Look up a blob by id.
    pub(super) fn lookup_blob(&self, blob_id: u32) -> Result<Arc<PropertyBlob>> {
        self.blobs
            .lock()
            .get(&blob_id)
            .cloned()
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown blob id"))
    }
}

/// `DRM_IOCTL_MODE_CREATEPROPBLOB`: create a property blob.
pub(super) fn create_property_blob(
    handle: &super::DriHandle,
    cmd: Ioctl<b'd', 0xbd, true, InOutData<DrmModeCreatePropertyBlob>>,
) -> Result<i32> {
    let mut req = cmd.read()?;
    let mut data = alloc::vec![0u8; req.length as usize];
    current_userspace!().read_bytes(req.data_ptr as usize, &mut data)?;
    let blob_id = handle.gpu_manager.property_manager.create_blob(data);
    req.blob_id = blob_id;
    cmd.write(&req)?;
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
        .destroy_blob(req.blob_id)?;
    Ok(0)
}
/// Converts a stored property value to the raw `u64` of the UAPI.
fn value_to_u64(value: &PropertyValue) -> u64 {
    match value {
        PropertyValue::Range(x) => *x,
        PropertyValue::SignedRange(x) => *x as u64,
        PropertyValue::Object(x) | PropertyValue::Blob(x) | PropertyValue::Enum(x) => *x as u64,
    }
}

/// The default value of a property that userspace has not explicitly set.
///
/// Our device has exactly one CRTC (id 1), one connector (id 1), and one
/// primary plane (id 1); the display is active from boot, so `ACTIVE`
/// defaults to 1 and the `CRTC_ID` references default to the CRTC.
fn default_value(prop: &Property) -> u64 {
    match prop.name {
        "ACTIVE" => 1,
        "CRTC_ID" => 1,
        "type" => DRM_PLANE_TYPE_PRIMARY as u64,
        _ => 0,
    }
}

/// Copies a NUL-padded name into a fixed `DRM_PROP_NAME_LEN` field.
fn copy_name(name: &str, out: &mut [u8; DRM_PROP_NAME_LEN]) {
    let len = name.len().min(DRM_PROP_NAME_LEN - 1);
    out[..len].copy_from_slice(&name.as_bytes()[..len]);
}

/// `DRM_IOCTL_MODE_OBJ_GETPROPERTIES`: list an object's properties and values.
pub(super) fn get_obj_properties(
    handle: &super::DriHandle,
    cmd: Ioctl<b'd', 0xb9, true, InOutData<DrmModeObjGetProperties>>,
) -> Result<i32> {
    let mut req = cmd.read()?;
    let prop_mgr = &handle.gpu_manager.property_manager;

    // All our objects (CRTC, connector, plane) share id 1; other ids are
    // reported as having no properties.
    let prop_ids: &[u32] = if req.obj_id == 1 {
        prop_mgr.property_ids_for_object(req.obj_type)
    } else {
        &[]
    };

    let total = prop_ids.len() as u32;
    if req.props_ptr != 0 && req.prop_values_ptr != 0 {
        let count = (req.count_props as usize).min(prop_ids.len());
        for (i, prop_id) in prop_ids[..count].iter().enumerate() {
            let prop = prop_mgr.lookup_property(*prop_id)?;
            let value = prop_mgr
                .get_value(req.obj_id, req.obj_type, *prop_id)
                .map(|v| value_to_u64(&v))
                .unwrap_or_else(|| default_value(&prop));
            current_userspace!()
                .write_val(req.props_ptr as usize + i * size_of::<u32>(), prop_id)?;
            current_userspace!()
                .write_val(req.prop_values_ptr as usize + i * size_of::<u64>(), &value)?;
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

    req.flags = prop.prop_type.uapi_flags() | prop.flags;
    copy_name(prop.name, &mut req.name);
    req.count_values = 0;
    req.count_enum_blobs = 0;

    match prop.prop_type {
        PropertyType::Range | PropertyType::SignedRange => {
            req.count_values = 2;
            if req.values_ptr != 0 {
                current_userspace!().write_val(req.values_ptr as usize, &prop.min)?;
                current_userspace!()
                    .write_val(req.values_ptr as usize + size_of::<u64>(), &prop.max)?;
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
                for (i, (value, name)) in PLANE_TYPES.iter().enumerate() {
                    let mut entry = super::DrmModePropertyEnum {
                        value: *value,
                        name: [0; DRM_PROP_NAME_LEN],
                    };
                    copy_name(name, &mut entry.name);
                    current_userspace!().write_val(
                        req.enum_blob_ptr as usize + i * size_of::<super::DrmModePropertyEnum>(),
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

    if req.data != 0 && (req.length as usize) >= blob.data.len() {
        current_userspace!().write_bytes(req.data as usize, &blob.data[..])?;
    }
    req.length = blob.data.len() as u32;
    cmd.write(&req)?;
    Ok(0)
}
