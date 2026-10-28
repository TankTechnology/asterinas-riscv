// SPDX-License-Identifier: MPL-2.0

//! The device model: devices, buses, classes, and drivers, and the sysfs view built from them.
//!
//! This crate manages devices and their parent-child relationships,
//! matches devices with drivers through buses,
//! presents them to user space through classes,
//! and shows all of it under `/sys`.
//! It sits between the hardware components (which enumerate devices and implement drivers)
//! and the `systree` component (which sysfs displays),
//! and it owns every node under `/sys/devices`, `/sys/bus`, `/sys/class`, and `/sys/dev`.
//!
//! Navigate to the module that fits the task at hand:
//!
//! - If you are adding a bus or a bus driver, see [`bus`].
//! - If you are adding a class or a class device, see [`class`].
//! - If you are interested in the machinery every device shares, see [`common`].
//! - If you are tracing how a device becomes a `/dev` node, see [`hooks`].
//!
//! Every device is registered to or unregistered from the device model
//! through [`add_device`] and [`remove_device`].

#![no_std]
#![deny(unsafe_code)]

extern crate alloc;

// Set this crate's log prefix for `ostd::log`.
macro_rules! __log_prefix {
    () => {
        "device: "
    };
}

pub mod bus;
pub mod class;
pub mod common;
pub mod hooks;

/// Attaches a subtree under the device model's `/sys/dev/char`.
///
/// This is the escape hatch for a subsystem that publishes its own
/// character-device view instead of registering devices through the model.
/// The device model owns `/sys/dev`; the caller owns the subtree, including its
/// name, and must attach it at most once.
///
/// # Panics
///
/// Panics if the device model is not initialized.
pub fn register_legacy_dev_entry(
    node: alloc::sync::Arc<dyn aster_systree::SysObj>,
) -> common::Result<()> {
    registry::get().attach_into_dev_index(common::DevKind::Char, node)
}

/// Attaches a directory under the device model's `/sys/class`.
///
/// The companion of [`register_legacy_dev_entry`] for the class view.
///
/// # Panics
///
/// Panics if the device model is not initialized.
pub fn register_legacy_class_dir(
    dir: alloc::sync::Arc<dyn aster_systree::SysObj>,
) -> common::Result<()> {
    registry::get().attach_into_class_root(dir)
}

/// Attaches a directory under the device model's `/sys/bus`.
///
/// The companion of [`register_legacy_dev_entry`] for the bus view.
///
/// # Panics
///
/// Panics if the device model is not initialized.
pub fn register_legacy_bus_dir(
    dir: alloc::sync::Arc<dyn aster_systree::SysObj>,
) -> common::Result<()> {
    registry::get().attach_into_bus_root(dir)
}
#[cfg(ktest)]
mod test;

use component::{ComponentInitError, init_component};

pub use self::common::registration::{
    add as add_device, register_legacy_virtual_node, remove as remove_device,
};
use self::common::registry;

/// Initializes the device model for kernel-mode tests.
#[cfg(ktest)]
pub fn init_for_ktest() {
    aster_systree::init_for_ktest();
    registry::init();
}

#[init_component]
fn init() -> Result<(), ComponentInitError> {
    registry::init();
    Ok(())
}
