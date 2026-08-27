// SPDX-License-Identifier: MPL-2.0

//! DRM atomic modesetting ioctl support.
//!
//! Parses the Linux `drm_mode_atomic` object/property wire format.
//! It validates the complete request before updating the single virtio-gpu KMS pipeline.
//! The parser keeps Linux's per-object property counts
//! instead of exposing a driver-specific flattened ABI.

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

/// Complete software state proposed by one atomic request.
///
/// This is built from the committed property state before applying any user
/// updates. Validation and hardware decisions therefore observe one coherent
/// state instead of interpreting each property in isolation.
#[derive(Clone, Debug)]
struct ProposedKmsState {
    active: bool,
    mode: Option<super::property::PropertyBlobRef>,
    connector_crtc: Option<u32>,
    plane_fb: Option<u32>,
    plane_crtc: Option<u32>,
    src_x: u64,
    src_y: u64,
    src_w: u64,
    src_h: u64,
    crtc_x: i64,
    crtc_y: i64,
    crtc_w: u64,
    crtc_h: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum AtomicHardwareUpdate {
    None,
    Present(u32),
    Disable,
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
    let current_state = ProposedKmsState::from_committed(handle)?;
    let mut proposed_state = current_state.clone();
    proposed_state.apply_updates(&updates)?;
    let hardware_update = validate_atomic_state(
        handle,
        kms_state,
        &current_state,
        &proposed_state,
        &updates,
        req.flags,
    )?;
    if req.flags & DRM_MODE_ATOMIC_TEST_ONLY != 0 {
        return Ok(0);
    }

    commit_atomic_state(
        handle,
        kms_state,
        &updates,
        hardware_update,
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
        PropertyType::Enum => PropertyValue::Enum(
            u32::try_from(value)
                .map_err(|_| Error::with_message(Errno::EINVAL, "enum property value overflows"))?,
        ),
    };
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

impl ProposedKmsState {
    fn empty() -> Self {
        Self {
            active: false,
            mode: None,
            connector_crtc: None,
            plane_fb: None,
            plane_crtc: None,
            src_x: 0,
            src_y: 0,
            src_w: 0,
            src_h: 0,
            crtc_x: 0,
            crtc_y: 0,
            crtc_w: 0,
            crtc_h: 0,
        }
    }

    fn from_committed(handle: &super::DriHandle) -> Result<Self> {
        let property_manager = &handle.gpu_manager.property_manager;
        let mut state = Self::empty();
        for object in [
            AtomicKmsObject::Crtc,
            AtomicKmsObject::Connector,
            AtomicKmsObject::PrimaryPlane,
        ] {
            for property_id in property_manager.property_ids_for_object(object.object_type()) {
                let property = property_manager.lookup_property(*property_id)?;
                let value =
                    property_manager.current_value(object.id(), object.object_type(), &property);
                state.apply_property(property.kind, &value)?;
            }
        }
        Ok(state)
    }

    fn apply_updates(&mut self, updates: &[AtomicObjectUpdate]) -> Result<()> {
        for update in updates {
            for property_update in &update.properties {
                self.apply_property(property_update.property.kind, &property_update.value)?;
            }
        }
        Ok(())
    }

    fn apply_property(&mut self, kind: PropertyKind, value: &PropertyValue) -> Result<()> {
        match (kind, value) {
            (PropertyKind::Active, PropertyValue::Range(value)) => self.active = *value != 0,
            (PropertyKind::ModeId, PropertyValue::Blob(blob)) => self.mode = blob.clone(),
            (PropertyKind::ConnectorCrtcId, PropertyValue::Object(id)) => {
                self.connector_crtc = (*id != 0).then_some(*id);
            }
            (PropertyKind::PlaneFbId, PropertyValue::Object(id)) => {
                self.plane_fb = (*id != 0).then_some(*id);
            }
            (PropertyKind::PlaneCrtcId, PropertyValue::Object(id)) => {
                self.plane_crtc = (*id != 0).then_some(*id);
            }
            (PropertyKind::SrcX, PropertyValue::Range(value)) => self.src_x = *value,
            (PropertyKind::SrcY, PropertyValue::Range(value)) => self.src_y = *value,
            (PropertyKind::SrcW, PropertyValue::Range(value)) => self.src_w = *value,
            (PropertyKind::SrcH, PropertyValue::Range(value)) => self.src_h = *value,
            (PropertyKind::CrtcX, PropertyValue::SignedRange(value)) => self.crtc_x = *value,
            (PropertyKind::CrtcY, PropertyValue::SignedRange(value)) => self.crtc_y = *value,
            (PropertyKind::CrtcW, PropertyValue::Range(value)) => self.crtc_w = *value,
            (PropertyKind::CrtcH, PropertyValue::Range(value)) => self.crtc_h = *value,
            (PropertyKind::PlaneType, PropertyValue::Enum(_)) => {}
            _ => {
                return_errno_with_message!(
                    Errno::EINVAL,
                    "property value does not match its internal kind"
                );
            }
        }
        Ok(())
    }

    fn mode_id(&self) -> Option<u32> {
        self.mode.as_ref().map(|blob| blob.id())
    }

    fn modeset_changed_from(&self, current: &Self) -> bool {
        self.active != current.active
            || self.mode_id() != current.mode_id()
            || self.connector_crtc != current.connector_crtc
    }

    fn scanout_framebuffer(&self) -> Option<u32> {
        if self.active { self.plane_fb } else { None }
    }

    fn validate_topology(&self) -> Result<()> {
        let crtc_enabled = self.mode.is_some();
        let connector_attached = self.connector_crtc.is_some();
        if crtc_enabled != connector_attached {
            return_errno_with_message!(
                Errno::EINVAL,
                "CRTC mode and connector routing must be enabled together"
            );
        }
        if self.active && !crtc_enabled {
            return_errno_with_message!(Errno::EINVAL, "active CRTC has no mode or connector");
        }
        if self.plane_fb.is_some() != self.plane_crtc.is_some() {
            return_errno_with_message!(Errno::EINVAL, "plane FB and CRTC must be set together");
        }
        Ok(())
    }

    fn validate(&self, handle: &super::DriHandle) -> Result<()> {
        self.validate_topology()?;
        let mode = self.mode.as_ref().map(validate_mode_blob).transpose()?;
        let framebuffer = if let Some(framebuffer_id) = self.plane_fb {
            Some(
                *handle
                    .inner
                    .lock()
                    .framebuffers
                    .get(&framebuffer_id)
                    .ok_or_else(|| {
                        Error::with_message(Errno::EINVAL, "unknown scanout framebuffer id")
                    })?,
            )
        } else {
            None
        };
        if self.active && framebuffer.is_none() {
            return_errno_with_message!(Errno::EINVAL, "active CRTC has no primary plane");
        }
        if let Some(framebuffer) = framebuffer {
            self.validate_plane_geometry(&framebuffer)?;
            if let Some(mode) = mode
                && (u32::from(mode.hdisplay) != framebuffer.width
                    || u32::from(mode.vdisplay) != framebuffer.height)
            {
                return_errno_with_message!(
                    Errno::EINVAL,
                    "mode, plane, and framebuffer dimensions differ"
                );
            }
        }
        Ok(())
    }

    fn validate_plane_geometry(&self, framebuffer: &super::Framebuffer) -> Result<()> {
        let source_width = u64::from(framebuffer.width) << 16;
        let source_height = u64::from(framebuffer.height) << 16;
        if self.src_x != 0
            || self.src_y != 0
            || self.src_w != source_width
            || self.src_h != source_height
            || self.crtc_x != 0
            || self.crtc_y != 0
            || self.crtc_w != u64::from(framebuffer.width)
            || self.crtc_h != u64::from(framebuffer.height)
        {
            return_errno_with_message!(
                Errno::EOPNOTSUPP,
                "only full-frame scanout without cropping or scaling is supported"
            );
        }
        Ok(())
    }
}

fn validate_atomic_state(
    handle: &super::DriHandle,
    kms_state: &super::KmsState,
    current_state: &ProposedKmsState,
    proposed_state: &ProposedKmsState,
    updates: &[AtomicObjectUpdate],
    flags: u32,
) -> Result<AtomicHardwareUpdate> {
    proposed_state.validate(handle)?;
    if proposed_state.modeset_changed_from(current_state)
        && flags & DRM_MODE_ATOMIC_ALLOW_MODESET == 0
    {
        return_errno_with_message!(Errno::EINVAL, "mode change requires ALLOW_MODESET");
    }

    if flags & DRM_MODE_PAGE_FLIP_EVENT != 0 {
        let has_crtc_target = updates.iter().any(|update| {
            !update.properties.is_empty() && object_routes_to_crtc(update.object, proposed_state)
        });
        if !has_crtc_target {
            return_errno_with_message!(Errno::EINVAL, "atomic event has no CRTC");
        }
        if !current_state.active && !proposed_state.active {
            return_errno_with_message!(Errno::EINVAL, "atomic event targets an inactive CRTC");
        }
        if flags & DRM_MODE_ATOMIC_TEST_ONLY == 0 {
            handle.check_flip_event_capacity()?;
        }
    }

    if updates.is_empty() {
        return Ok(AtomicHardwareUpdate::None);
    }
    if let Some(framebuffer_id) = proposed_state.scanout_framebuffer() {
        return Ok(AtomicHardwareUpdate::Present(framebuffer_id));
    }
    if kms_state.scanout.is_some() {
        return Ok(AtomicHardwareUpdate::Disable);
    }
    Ok(AtomicHardwareUpdate::None)
}

/// Returns whether changing `object` affects the single exposed CRTC.
///
/// Atomic page flips commonly update only a plane's `FB_ID`. Such a plane
/// still targets its already-committed CRTC and must be allowed to request a
/// page-flip event. Connector updates similarly affect the CRTC to which the
/// connector is routed.
fn object_routes_to_crtc(object: AtomicKmsObject, state: &ProposedKmsState) -> bool {
    match object {
        AtomicKmsObject::Crtc => true,
        AtomicKmsObject::Connector => state.connector_crtc == Some(CRTC_ID),
        AtomicKmsObject::PrimaryPlane => state.plane_crtc == Some(CRTC_ID),
    }
}

fn commit_atomic_state(
    handle: &super::DriHandle,
    kms_state: &mut super::KmsState,
    updates: &[AtomicObjectUpdate],
    hardware_update: AtomicHardwareUpdate,
    flags: u32,
    user_data: u64,
) -> Result<i32> {
    let property_manager = &handle.gpu_manager.property_manager;

    // Device presentation is the only fallible state change. Perform it
    // before publishing property values so a failed update cannot leave a
    // partially committed software state.
    match hardware_update {
        AtomicHardwareUpdate::None => {}
        AtomicHardwareUpdate::Present(framebuffer_id) => {
            kms::present_fb(handle, kms_state, framebuffer_id)?;
        }
        AtomicHardwareUpdate::Disable => {
            handle
                .gpu_manager
                .gpu
                .disable_scanout()
                .map_err(|_| Error::with_message(Errno::EIO, "virtio-gpu disable failed"))?;
            kms_state.scanout = None;
        }
    }

    property_manager.set_values(updates.iter().flat_map(|update| {
        update.properties.iter().map(|property_update| {
            (
                update.object.id(),
                update.object.object_type(),
                property_update.property.id,
                &property_update.value,
            )
        })
    }));

    if flags & DRM_MODE_PAGE_FLIP_EVENT != 0 {
        handle.queue_flip_event(user_data)?;
    }
    Ok(0)
}

/// Decodes and validates one exact `drm_mode_modeinfo` blob.
fn validate_mode_blob(blob: &super::property::PropertyBlobRef) -> Result<DrmModeModeInfo> {
    if blob.data().len() != size_of::<DrmModeModeInfo>() {
        return_errno_with_message!(Errno::EINVAL, "mode blob has an invalid size");
    }
    let mode = DrmModeModeInfo::from_first_bytes(blob.data());
    if mode.clock == 0
        || mode.hdisplay == 0
        || mode.hdisplay > mode.hsync_start
        || mode.hsync_start > mode.hsync_end
        || mode.hsync_end > mode.htotal
        || mode.vdisplay == 0
        || mode.vdisplay > mode.vsync_start
        || mode.vsync_start > mode.vsync_end
        || mode.vsync_end > mode.vtotal
    {
        return_errno_with_message!(Errno::EINVAL, "mode timings are invalid");
    }
    Ok(mode)
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

    #[ktest]
    fn proposed_state_rejects_incoherent_topology() {
        let mut state = ProposedKmsState::empty();
        assert!(state.validate_topology().is_ok());

        state.active = true;
        assert!(state.validate_topology().is_err());
        state.active = false;

        state.connector_crtc = Some(CRTC_ID);
        assert!(state.validate_topology().is_err());
        state.connector_crtc = None;

        state.plane_fb = Some(7);
        assert!(state.validate_topology().is_err());
        state.plane_crtc = Some(CRTC_ID);
        assert!(state.validate_topology().is_ok());
    }

    #[ktest]
    fn drm_validation_routed_plane_is_a_valid_atomic_event_target() {
        let mut state = ProposedKmsState::empty();
        assert!(!object_routes_to_crtc(
            AtomicKmsObject::PrimaryPlane,
            &state
        ));

        state.plane_crtc = Some(CRTC_ID);
        assert!(object_routes_to_crtc(AtomicKmsObject::PrimaryPlane, &state));
        assert!(object_routes_to_crtc(AtomicKmsObject::Crtc, &state));
    }
}
