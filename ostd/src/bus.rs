// SPDX-License-Identifier: MPL-2.0

//! Bus probe error

#[cfg(target_arch = "riscv64")]
pub mod usb;

// TODO: Implement a bus component and move the `BusProbeError` into the module.

/// An error that occurs during bus probing.
#[derive(Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum BusProbeError {
    /// The device does not match the expected criteria.
    DeviceNotMatch,
    /// An error in accessing the configuration space of the device.
    ConfigurationSpaceError,
}
