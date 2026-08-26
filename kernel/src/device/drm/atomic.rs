// SPDX-License-Identifier: MPL-2.0

//! Atomic modesetting ioctl handler.
//!
//! Implements `DRM_IOCTL_MODE_ATOMIC` with support for:
//! - `DRM_MODE_ATOMIC_TEST_ONLY` — validate-only mode
//! - `DRM_MODE_ATOMIC_ALLOW_MODESET` — mode changes (MODE_ID blob)
//! - `DRM_MODE_PAGE_FLIP_EVENT` — queue a flip-completion event on commit
//! - Framebuffer commits (FB_ID → `present_fb`)

use ostd::mm::VmIo;

use super::{
    DRM_MODE_ATOMIC_ALLOW_MODESET, DRM_MODE_ATOMIC_NONBLOCK, DRM_MODE_ATOMIC_TEST_ONLY,
    DRM_MODE_OBJECT_CONNECTOR, DRM_MODE_OBJECT_CRTC, DRM_MODE_OBJECT_PLANE,
    DRM_MODE_PAGE_FLIP_EVENT, DrmModeAtomic, kms,
    property::{PropertyManager, PropertyType, PropertyValue},
};
use crate::{
    context::current_userspace,
    prelude::*,
    util::ioctl::{InOutData, Ioctl},
};

/// `DRM_IOCTL_MODE_ATOMIC`: atomic commit of KMS property changes.
pub(super) fn mode_atomic(
    handle: &super::DriHandle,
    kms_state: &mut super::KmsState,
    cmd: Ioctl<b'd', 0xbc, true, InOutData<DrmModeAtomic>>,
) -> Result<i32> {
    let req = cmd.read()?;
    let flags = req.flags;
    let count_props = req.count_props as usize;

    if flags
        & !(DRM_MODE_ATOMIC_TEST_ONLY
            | DRM_MODE_ATOMIC_NONBLOCK
            | DRM_MODE_ATOMIC_ALLOW_MODESET
            | DRM_MODE_PAGE_FLIP_EVENT)
        != 0
    {
        return_errno_with_message!(Errno::EINVAL, "unsupported atomic flags");
    }

    if count_props == 0 {
        return Ok(0);
    }

    if count_props > 64 {
        return_errno_with_message!(Errno::EINVAL, "too many atomic properties");
    }

    let prop_mgr = &handle.gpu_manager.property_manager;

    // Read arrays from userspace
    let mut obj_ids = alloc::vec![0u32; count_props];
    let mut prop_ids = alloc::vec![0u32; count_props];
    let mut prop_values = alloc::vec![0u64; count_props];

    if req.objs_ptr != 0 {
        let mut raw = alloc::vec![0u8; count_props * 4];
        current_userspace!().read_bytes(req.objs_ptr as usize, &mut raw)?;
        for (i, chunk) in raw.as_chunks::<4>().0.iter().enumerate() {
            obj_ids[i] = u32::from_le_bytes(*chunk);
        }
    }
    if req.props_ptr != 0 {
        let mut raw = alloc::vec![0u8; count_props * 4];
        current_userspace!().read_bytes(req.props_ptr as usize, &mut raw)?;
        for (i, chunk) in raw.as_chunks::<4>().0.iter().enumerate() {
            prop_ids[i] = u32::from_le_bytes(*chunk);
        }
    }
    if req.prop_values_ptr != 0 {
        let mut raw = alloc::vec![0u8; count_props * 8];
        current_userspace!().read_bytes(req.prop_values_ptr as usize, &mut raw)?;
        for (i, chunk) in raw.as_chunks::<8>().0.iter().enumerate() {
            prop_values[i] = u64::from_le_bytes(*chunk);
        }
    }

    // Validate all property changes
    for i in 0..count_props {
        let obj_id = obj_ids[i];
        let prop_id = prop_ids[i];
        let val = prop_values[i];

        let prop = prop_mgr.lookup_property(prop_id)?;
        // All our objects (CRTC, connector, plane) share id 1; the property
        // must apply to at least one object type.
        if obj_id != 1 {
            return_errno_with_message!(Errno::EINVAL, "unknown object id");
        }
        let applies = [
            DRM_MODE_OBJECT_CRTC,
            DRM_MODE_OBJECT_CONNECTOR,
            DRM_MODE_OBJECT_PLANE,
        ]
        .into_iter()
        .any(|obj_type| prop_mgr.property_applies(obj_type, prop_id));
        if !applies {
            return_errno_with_message!(Errno::EINVAL, "property does not apply to any object");
        }

        validate_property_value(prop_mgr, &prop, val)?;
    }

    // If TEST_ONLY, return success without committing
    if flags & DRM_MODE_ATOMIC_TEST_ONLY != 0 {
        return Ok(0);
    }

    // Commit the atomic changes
    commit_atomic_state(
        handle,
        kms_state,
        prop_mgr,
        &obj_ids,
        &prop_ids,
        &prop_values,
        flags,
        req.user_data,
    )
}

/// Validate a single property value against its type and range.
fn validate_property_value(
    prop_mgr: &PropertyManager,
    prop: &super::property::Property,
    val: u64,
) -> Result<()> {
    match prop.prop_type {
        PropertyType::Range => {
            if val < prop.min || val > prop.max {
                return_errno_with_message!(Errno::EINVAL, "property value out of range");
            }
        }
        PropertyType::SignedRange => {
            let sval = val as i64;
            let smin = prop.min as i64;
            let smax = prop.max as i64;
            if sval < smin || sval > smax {
                return_errno_with_message!(Errno::EINVAL, "property value out of signed range");
            }
        }
        PropertyType::Blob => {
            if val != 0 {
                prop_mgr.lookup_blob(val as u32)?;
            }
        }
        PropertyType::Object => {
            // Object references are validated loosely — 0 means "none"
        }
        PropertyType::Enum => {
            // No strict validation — the value format is opaque to the kernel
        }
    }
    Ok(())
}

/// Commit validated atomic property changes.
fn commit_atomic_state(
    handle: &super::DriHandle,
    kms_state: &mut super::KmsState,
    prop_mgr: &PropertyManager,
    obj_ids: &[u32],
    prop_ids: &[u32],
    prop_values: &[u64],
    flags: u32,
    user_data: u64,
) -> Result<i32> {
    let mut new_fb_id: Option<u32> = None;
    let mut new_mode_blob: Option<u32> = None;

    for i in 0..obj_ids.len() {
        let prop_id = prop_ids[i];
        let val = prop_values[i];

        let prop = prop_mgr.lookup_property(prop_id)?;

        match prop.name {
            "ACTIVE" => {
                prop_mgr.set_value(1, DRM_MODE_OBJECT_CRTC, prop_id, PropertyValue::Range(val));
            }
            "MODE_ID" => {
                new_mode_blob = if val == 0 { None } else { Some(val as u32) };
                prop_mgr.set_value(
                    1,
                    DRM_MODE_OBJECT_CRTC,
                    prop_id,
                    PropertyValue::Blob(val as u32),
                );
            }
            "FB_ID" => {
                new_fb_id = if val == 0 { None } else { Some(val as u32) };
                prop_mgr.set_value(
                    1,
                    DRM_MODE_OBJECT_PLANE,
                    prop_id,
                    PropertyValue::Object(val as u32),
                );
            }
            "CRTC_ID" => {
                prop_mgr.set_value(
                    1,
                    DRM_MODE_OBJECT_PLANE,
                    prop_id,
                    PropertyValue::Object(val as u32),
                );
            }
            "SRC_X" | "SRC_Y" | "SRC_W" | "SRC_H" => {
                prop_mgr.set_value(1, DRM_MODE_OBJECT_PLANE, prop_id, PropertyValue::Range(val));
            }
            "CRTC_X" | "CRTC_Y" | "CRTC_W" | "CRTC_H" => {
                prop_mgr.set_value(
                    1,
                    DRM_MODE_OBJECT_PLANE,
                    prop_id,
                    PropertyValue::SignedRange(val as i64),
                );
            }
            "type" => {
                prop_mgr.set_value(
                    1,
                    DRM_MODE_OBJECT_PLANE,
                    prop_id,
                    PropertyValue::Enum(val as u32),
                );
            }
            _ => {}
        }
    }

    // If ALLOW_MODESET, apply the mode blob
    if flags & DRM_MODE_ATOMIC_ALLOW_MODESET != 0
        && let Some(blob_id) = new_mode_blob
    {
        validate_mode_blob(prop_mgr, blob_id)?;
    }

    // If we have a FB_ID, present it
    if let Some(fb_id) = new_fb_id {
        kms::present_fb(handle, kms_state, fb_id)?;
        if flags & DRM_MODE_PAGE_FLIP_EVENT != 0 {
            handle.queue_flip_event(user_data)?;
        }
    }

    Ok(0)
}

/// Checks that a mode blob contains a complete `drm_mode_modeinfo`.
fn validate_mode_blob(prop_mgr: &PropertyManager, blob_id: u32) -> Result<()> {
    let blob = prop_mgr.lookup_blob(blob_id)?;
    // The blob data is a `struct drm_mode_modeinfo` (68 bytes on RISC-V).
    if blob.data.len() < 68 {
        return_errno_with_message!(Errno::EINVAL, "mode blob too small");
    }
    Ok(())
}
