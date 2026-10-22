// SPDX-License-Identifier: MPL-2.0

//! The Direct Rendering Manager subsystem of Asterinas.
//!
//! This crate provides the kernel-side framework for exposing graphics devices
//! through the Linux-compatible DRM userspace API. It sits between graphics
//! devices and the character-device layer, providing the shared object model,
//! lifecycle management, permission checks, and ioctl infrastructure needed by
//! DRM devices.
//!
//! Graphics devices implement [`device::DrmDevice`] and the relevant operation
//! traits to supply hardware-specific behavior. The DRM core owns the common
//! userspace-facing semantics and coordinates access to the device, keeping
//! policy and ABI handling independent of individual device implementations.

#![no_std]
#![deny(unsafe_code)]

use alloc::{format, sync::Arc, vec::Vec};

use aster_core::{
    device::{Device, registry::char},
    prelude::*,
    process::{UserNamespace, credentials::capabilities::CapSet, posix_thread::AsPosixThread},
    security::lsm::hooks::{self as lsm_hook, CapableContext},
};
use ostd::task::Task;

use crate::{
    device::{DrmDevice, DrmFeatures, RegisteredDrmDevice},
    minor::{DrmMinor, DrmMinorType},
};

extern crate alloc;
#[macro_use]
extern crate ostd_pod;

// Sets this crate's log prefix for `ostd::log`.
macro_rules! __log_prefix {
    () => {
        "drm: "
    };
}

pub mod device;
mod event;
mod fence;
mod file;
pub mod gem;
mod prime;
mod sync_file;
mod syncobj;
mod ioctl;
pub mod kms;
mod minor;
pub mod utils;

/// The DRM character-device major (`DRM_MAJOR` in Linux).
const DRM_MAJOR: u16 = 226;

pub fn register_device(device: Arc<dyn DrmDevice>) -> Result<()> {
    let registered_device = Arc::new(RegisteredDrmDevice::new(device)?);
    let index = registered_device.index();
    let render_minor = if registered_device.device().has_features(DrmFeatures::RENDER) {
        let minor = DrmMinor::new(registered_device.clone(), DrmMinorType::Render);
        char::register(minor.clone())?;
        Some(minor)
    } else {
        None
    };

    let primary_minor = DrmMinor::new(registered_device.clone(), DrmMinorType::Primary);

    if let Err(error) = char::register(primary_minor) {
        if let Some(render_minor) = render_minor {
            let _ = char::unregister(render_minor.id());
        }
        return Err(error);
    }

    // libdrm needs the /sys/dev/char view of the nodes before it hands the
    // device to Mesa; without it GBM (and therefore DRI3) cannot open it.
    let card_name = format!("card{index}");
    let render_name = format!("renderD{}", index + 128);
    let mut nodes: Vec<(&str, u32)> = alloc::vec![(card_name.as_str(), index)];
    if render_minor.is_some() {
        nodes.push((render_name.as_str(), index + 128));
    }
    let driver = registered_device.device().name();
    let modalias = registered_device.device().sysfs_modalias().unwrap_or("");
    if let Err(error) =
        aster_core::device::drm_sysfs::register_drm_device(DRM_MAJOR, &nodes, driver, modalias)
    {
        ostd::warn!("failed to publish the DRM sysfs nodes: {:?}", error);
    }

    Ok(())
}

fn has_current_sys_admin() -> bool {
    let task = Task::current().unwrap();
    let posix_thread = task.as_posix_thread().unwrap();

    lsm_hook::on_capable(CapableContext::new(
        UserNamespace::get_init_singleton().as_ref(),
        posix_thread,
        CapSet::SYS_ADMIN,
    ))
    .is_ok()
}
