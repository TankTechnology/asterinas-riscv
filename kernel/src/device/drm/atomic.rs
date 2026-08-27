// SPDX-License-Identifier: MPL-2.0

//! DRM atomic modesetting ioctl support.
//!
//! Parses the Linux `drm_mode_atomic` object/property wire format.
//! It validates the complete request before updating the single virtio-gpu KMS pipeline.
//! The parser keeps Linux's per-object property counts instead of exposing a
//! driver-specific flattened ABI.

use ostd::mm::VmIo;

use super::{
    AtomicKmsObject, CRTC_ID, DRM_MODE_ATOMIC_ALLOW_MODESET, DRM_MODE_ATOMIC_NONBLOCK,
    DRM_MODE_ATOMIC_TEST_ONLY, DRM_MODE_PAGE_FLIP_EVENT, DrmModeAtomic, DrmModeModeInfo, kms,
    property::{Property, PropertyKind, PropertyType, PropertyValue},
};
use crate::{
    context::current_userspace,
    prelude::*,
    util::ioctl::{InOutData, Ioctl},
};

/// The device exposes exactly three atomic-capable objects.
const MAX_ATOMIC_OBJECTS: usize = 3;
/// Bounds allocation and validation work performed by one atomic ioctl.
const MAX_ATOMIC_PROPERTIES: usize = 64;

#[derive(Debug)]
struct AtomicPropertyUpdate {
    property: Arc<Property>,
    value: PropertyValue,
}

#[derive(Debug)]
struct AtomicObjectUpdate {
    object: AtomicKmsObject,
    properties: Vec<AtomicPropertyUpdate>,
}

/// Applies one validated atomic KMS transaction.
pub(super) fn mode_atomic(
    handle: &super::DriHandle,
    kms_state: &mut super::KmsState,
    cmd: Ioctl<b'd', 0xbc, true, InOutData<DrmModeAtomic>>,
) -> Result<i32> {
    let req = cmd.read()?;
    validate_request_header(&req)?;

    let updates = read_and_validate_updates(handle, &req)?;
    let framebuffer_id = validate_atomic_state(handle, &updates, req.flags)?;
    if req.flags & DRM_MODE_ATOMIC_TEST_ONLY != 0 {
        return Ok(0);
    }

    commit_atomic_state(
        handle,
        kms_state,
        &updates,
        framebuffer_id,
        req.flags,
        req.user_data,
    )
}

fn validate_request_header(req: &DrmModeAtomic) -> Result<()> {
    if req.flags
        & !(DRM_MODE_ATOMIC_TEST_ONLY
            | DRM_MODE_ATOMIC_NONBLOCK
            | DRM_MODE_ATOMIC_ALLOW_MODESET
            | DRM_MODE_PAGE_FLIP_EVENT)
        != 0
    {
        return_errno_with_message!(Errno::EINVAL, "unsupported atomic flags");
    }
    if req.reserved != 0 {
        return_errno_with_message!(Errno::EINVAL, "atomic reserved field must be zero");
    }
    if req.flags & DRM_MODE_ATOMIC_NONBLOCK != 0 {
        return_errno_with_message!(
            Errno::EOPNOTSUPP,
            "nonblocking atomic commits are not implemented"
        );
    }
    if req.count_objs as usize > MAX_ATOMIC_OBJECTS {
        return_errno_with_message!(Errno::EINVAL, "too many atomic objects");
    }
    if req.count_objs != 0 && (req.objs_ptr == 0 || req.count_props_ptr == 0) {
        return_errno_with_message!(Errno::EFAULT, "atomic object arrays are null");
    }
    Ok(())
}

fn read_and_validate_updates(
    handle: &super::DriHandle,
    req: &DrmModeAtomic,
) -> Result<Vec<AtomicObjectUpdate>> {
    let object_count = req.count_objs as usize;
    if object_count == 0 {
        if req.flags & DRM_MODE_PAGE_FLIP_EVENT != 0 {
            return_errno_with_message!(Errno::EINVAL, "atomic event has no CRTC");
        }
        return Ok(Vec::new());
    }

    let object_ids = read_u32_array(req.objs_ptr, object_count)?;
    let property_counts = read_u32_array(req.count_props_ptr, object_count)?;
    let total_properties = property_counts.iter().try_fold(0usize, |total, count| {
        total
            .checked_add(*count as usize)
            .filter(|total| *total <= MAX_ATOMIC_PROPERTIES)
    });
    let total_properties = total_properties
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "too many atomic properties"))?;

    if total_properties != 0 && (req.props_ptr == 0 || req.prop_values_ptr == 0) {
        return_errno_with_message!(Errno::EFAULT, "atomic property arrays are null");
    }
    let property_ids = read_u32_array(req.props_ptr, total_properties)?;
    let property_values = read_u64_array(req.prop_values_ptr, total_properties)?;

    let property_manager = &handle.gpu_manager.property_manager;
    let mut seen_objects = BTreeSet::new();
    let mut property_offset = 0usize;
    let mut updates = Vec::with_capacity(object_count);

    for (index, object_id) in object_ids.into_iter().enumerate() {
        let object = AtomicKmsObject::from_id(object_id)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "unknown atomic object id"))?;
        if !seen_objects.insert(object_id) {
            return_errno_with_message!(Errno::EINVAL, "duplicate atomic object id");
        }

        let property_count = property_counts[index] as usize;
        let property_end = property_offset
            .checked_add(property_count)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "atomic property count overflows"))?;
        let mut seen_properties = BTreeSet::new();
        let mut object_updates = Vec::with_capacity(property_count);

        for property_index in property_offset..property_end {
            let property_id = property_ids[property_index];
            if !seen_properties.insert(property_id) {
                return_errno_with_message!(Errno::EINVAL, "duplicate property for atomic object");
            }

            let property = property_manager.lookup_property(property_id)?;
            if !property_manager.property_applies(object.object_type(), property_id) {
                return_errno_with_message!(
                    Errno::EINVAL,
                    "property does not apply to atomic object"
                );
            }
            let value =
                validate_property_value(handle, &property, property_values[property_index])?;
            object_updates.push(AtomicPropertyUpdate { property, value });
        }

        updates.push(AtomicObjectUpdate {
            object,
            properties: object_updates,
        });
        property_offset = property_end;
    }

    Ok(updates)
}

fn read_u32_array(pointer: u64, count: usize) -> Result<Vec<u32>> {
    if count == 0 {
        return Ok(Vec::new());
    }
    let byte_len = count
        .checked_mul(size_of::<u32>())
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "atomic u32 array size overflows"))?;
    let mut bytes = alloc::vec![0u8; byte_len];
    current_userspace!().read_bytes(pointer as usize, &mut bytes)?;
    Ok(bytes
        .as_chunks::<4>()
        .0
        .iter()
        .map(|bytes| u32::from_le_bytes(*bytes))
        .collect())
}

fn read_u64_array(pointer: u64, count: usize) -> Result<Vec<u64>> {
    if count == 0 {
        return Ok(Vec::new());
    }
    let byte_len = count
        .checked_mul(size_of::<u64>())
        .ok_or_else(|| Error::with_message(Errno::EINVAL, "atomic u64 array size overflows"))?;
    let mut bytes = alloc::vec![0u8; byte_len];
    current_userspace!().read_bytes(pointer as usize, &mut bytes)?;
    Ok(bytes
        .as_chunks::<8>()
        .0
        .iter()
        .map(|bytes| u64::from_le_bytes(*bytes))
        .collect())
}

fn validate_property_value(
    handle: &super::DriHandle,
    property: &Property,
    value: u64,
) -> Result<PropertyValue> {
    if property.is_immutable() {
        return_errno_with_message!(Errno::EINVAL, "immutable property cannot be changed");
    }

    let value = match property.prop_type {
        PropertyType::Range => {
            if value < property.min || value > property.max {
                return_errno_with_message!(Errno::EINVAL, "property value out of range");
            }
            PropertyValue::Range(value)
        }
        PropertyType::SignedRange => {
            let signed_value = value as i64;
            if signed_value < property.min as i64 || signed_value > property.max as i64 {
                return_errno_with_message!(Errno::EINVAL, "property value out of signed range");
            }
            PropertyValue::SignedRange(signed_value)
        }
        PropertyType::Blob => {
            let blob = if value == 0 {
                None
            } else {
                let blob_id = u32::try_from(value).map_err(|_| {
                    Error::with_message(Errno::EINVAL, "property blob id overflows")
                })?;
                Some(handle.gpu_manager.property_manager.lookup_blob(blob_id)?)
            };
            PropertyValue::Blob(blob)
        }
        PropertyType::Object => {
            validate_object_reference(handle, property.kind, value)?;
            PropertyValue::Object(u32::try_from(value).map_err(|_| {
                Error::with_message(Errno::EINVAL, "object property value overflows")
            })?)
        }
        PropertyType::Enum => {
            return_errno_with_message!(Errno::EINVAL, "unsupported mutable enum property");
        }
    };
    match (property.kind, &value) {
        (PropertyKind::Active, PropertyValue::Range(0))
        | (PropertyKind::ModeId, PropertyValue::Blob(None))
        | (
            PropertyKind::ConnectorCrtcId | PropertyKind::PlaneCrtcId | PropertyKind::PlaneFbId,
            PropertyValue::Object(0),
        ) => {
            return_errno_with_message!(
                Errno::EOPNOTSUPP,
                "atomic pipeline disable is not implemented"
            );
        }
        _ => {}
    }
    Ok(value)
}

fn validate_object_reference(
    handle: &super::DriHandle,
    property_kind: PropertyKind,
    value: u64,
) -> Result<()> {
    let referenced_id = u32::try_from(value)
        .map_err(|_| Error::with_message(Errno::EINVAL, "object property value overflows"))?;
    match property_kind {
        PropertyKind::ConnectorCrtcId | PropertyKind::PlaneCrtcId => {
            if referenced_id != 0 && referenced_id != CRTC_ID {
                return_errno_with_message!(Errno::EINVAL, "unknown CRTC object id");
            }
        }
        PropertyKind::PlaneFbId => {
            if referenced_id != 0
                && !handle
                    .inner
                    .lock()
                    .framebuffers
                    .contains_key(&referenced_id)
            {
                return_errno_with_message!(Errno::EINVAL, "unknown framebuffer id");
            }
        }
        _ => return_errno_with_message!(Errno::EINVAL, "invalid object-valued property"),
    }
    Ok(())
}

fn validate_atomic_state(
    handle: &super::DriHandle,
    updates: &[AtomicObjectUpdate],
    flags: u32,
) -> Result<Option<u32>> {
    let mut framebuffer_id = None;
    let mut mode_blob = None;

    for update in updates {
        for property_update in &update.properties {
            match (&property_update.property.kind, &property_update.value) {
                (PropertyKind::ModeId, PropertyValue::Blob(Some(blob))) => {
                    mode_blob = Some(blob);
                }
                (PropertyKind::PlaneFbId, PropertyValue::Object(id)) if *id != 0 => {
                    framebuffer_id = Some(*id);
                }
                _ => {}
            }
        }
    }

    if let Some(blob) = mode_blob {
        if flags & DRM_MODE_ATOMIC_ALLOW_MODESET == 0 {
            return_errno_with_message!(Errno::EINVAL, "mode change requires ALLOW_MODESET");
        }
        validate_mode_blob(blob)?;
    }
    if flags & DRM_MODE_PAGE_FLIP_EVENT != 0 {
        let includes_crtc = updates
            .iter()
            .any(|update| update.object == AtomicKmsObject::Crtc);
        if !includes_crtc {
            return_errno_with_message!(Errno::EINVAL, "atomic event has no CRTC");
        }
        if flags & DRM_MODE_ATOMIC_TEST_ONLY == 0 {
            handle.check_flip_event_capacity()?;
        }
    }

    Ok(framebuffer_id)
}

fn commit_atomic_state(
    handle: &super::DriHandle,
    kms_state: &mut super::KmsState,
    updates: &[AtomicObjectUpdate],
    framebuffer_id: Option<u32>,
    flags: u32,
    user_data: u64,
) -> Result<i32> {
    let property_manager = &handle.gpu_manager.property_manager;

    // Device presentation is the only fallible state change. Perform it
    // before publishing property values so a failed present cannot leave a
    // partially committed software state.
    if let Some(framebuffer_id) = framebuffer_id {
        kms::present_fb(handle, kms_state, framebuffer_id)?;
    }

    for update in updates {
        for property_update in &update.properties {
            property_manager.set_value(
                update.object.id(),
                update.object.object_type(),
                property_update.property.id,
                property_update.value.clone(),
            );
        }
    }

    if flags & DRM_MODE_PAGE_FLIP_EVENT != 0 {
        handle.queue_flip_event(user_data)?;
    }
    Ok(0)
}

/// Checks that a mode blob contains a complete `drm_mode_modeinfo`.
fn validate_mode_blob(blob: &super::property::PropertyBlobRef) -> Result<()> {
    if blob.data().len() < size_of::<DrmModeModeInfo>() {
        return_errno_with_message!(Errno::EINVAL, "mode blob too small");
    }
    Ok(())
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;
    use crate::device::drm::{CONNECTOR_ID, PRIMARY_PLANE_ID};

    #[ktest]
    fn atomic_uapi_layout_matches_linux() {
        assert_eq!(size_of::<DrmModeAtomic>(), 56);
        assert_eq!(size_of::<DrmModeModeInfo>(), 68);
    }

    #[ktest]
    fn atomic_objects_have_unique_ids_and_types() {
        let objects = [
            AtomicKmsObject::from_id(CRTC_ID).unwrap(),
            AtomicKmsObject::from_id(CONNECTOR_ID).unwrap(),
            AtomicKmsObject::from_id(PRIMARY_PLANE_ID).unwrap(),
        ];
        assert_eq!(objects[0], AtomicKmsObject::Crtc);
        assert_eq!(objects[1], AtomicKmsObject::Connector);
        assert_eq!(objects[2], AtomicKmsObject::PrimaryPlane);
        assert_ne!(CRTC_ID, CONNECTOR_ID);
        assert_ne!(CRTC_ID, PRIMARY_PLANE_ID);
        assert_ne!(CONNECTOR_ID, PRIMARY_PLANE_ID);
    }
}
