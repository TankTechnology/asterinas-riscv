// SPDX-License-Identifier: MPL-2.0

//! The `/sys/bus/usb` view of enumerated devices.
//!
//! The gate for increment 1 is a path: a hot-added stick must show up as
//! `/sys/bus/usb/devices/<busnum>-<port>` with `idVendor` and `idProduct`, the
//! same two attributes udev and every desktop stack read first.
//!
//! The subtree is built here and attached through the device model's legacy
//! bus entry point, exactly like the DRM sysfs view.

use alloc::{
    format,
    string::{String, ToString},
    sync::Arc,
    vec,
    vec::Vec,
};
use aster_systree::{
    AttrLessBranchNodeFields, BranchNodeFields, Error as SysTreeError,
    Result as SysTreeResult, SysAttrSetBuilder, SysBranchNode, SysObj, SysPerms, SysStr,
    inherit_sys_branch_node,
};
use aster_util::printer::VmPrinter;
use inherit_methods_macro::inherit_methods;
use ostd::{mm::VmWriter, sync::SpinLock, warn};

/// The `/sys/bus/usb/devices` directory, plus whether creation was attempted.
static DEVICES: SpinLock<(bool, Option<Arc<UsbBusDir>>)> = SpinLock::new((false, None));

/// Publishes one enumerated device under `/sys/bus/usb/devices`.
pub(super) fn publish_device(
    name: &str,
    vendor_id: u16,
    product_id: u16,
    bus_number: u8,
    device_number: u8,
    port: u8,
    speed: u32,
    configuration: u8,
    interfaces: usize,
    endpoints: &str,
) {
    let mut state = DEVICES.lock();
    if !state.0 {
        state.0 = true;
        match create_devices_dir() {
            Ok(dir) => state.1 = Some(dir),
            Err(error) => warn!("failed to build /sys/bus/usb: {:?}", error),
        }
    }
    let Some(devices) = state.1.clone() else {
        return;
    };
    drop(state);

    let attrs = vec![
        (
            "uevent",
            device_uevent(vendor_id, product_id, bus_number, device_number),
        ),
        ("idVendor", format!("{:04x}\n", vendor_id)),
        ("idProduct", format!("{:04x}\n", product_id)),
        ("busnum", format!("{:03}\n", bus_number)),
        ("devnum", format!("{:03}\n", device_number)),
        ("speed", format!("{}\n", speed_mbps(speed))),
        ("port", format!("{}\n", port)),
        ("bConfigurationValue", format!("{}\n", configuration)),
        ("bNumInterfaces", format!("{}\n", interfaces)),
        ("endpoints", endpoints.to_string()),
    ];
    let node = UsbDeviceNode::new(name, attrs);
    if let Err(error) = devices.add_child(node as Arc<dyn SysObj>) {
        warn!("failed to publish /sys/bus/usb/devices/{}: {:?}", name, error);
    }
}

/// The device's `uevent`, in the order Linux prints it.
fn device_uevent(vendor_id: u16, product_id: u16, bus_number: u8, device_number: u8) -> String {
    let minor = (bus_number as u32 - 1) * 128 + device_number as u32;
    format!(
        "MAJOR=189\nMINOR={minor}\nDEVNAME=bus/usb/{bus_number:03}/{device_number:03}\nDEVTYPE=usb_device\nDRIVER=usb\nPRODUCT={vendor_id:x}/{product_id:x}/100\nTYPE=0/0/0\nBUSNUM={bus_number:03}\nDEVNUM={device_number:03}\n"
    )
}

/// `PORTSC` speed encodings, in Mbit/s as Linux prints them.
fn speed_mbps(speed: u32) -> u32 {
    match speed {
        2 => 1,
        1 => 12,
        3 => 480,
        4 => 5000,
        _ => 0,
    }
}

fn create_devices_dir() -> SysTreeResult<Arc<UsbBusDir>> {
    let root = aster_systree::primary_tree().root();
    let bus_root = root.child("bus").ok_or(SysTreeError::NotFound)?;

    if bus_root
        .cast_to_branch()
        .map(|branch| branch.child("usb").is_some())
        .unwrap_or(false)
    {
        // Someone else owns the bus directory; do not fight over it.
        return Err(SysTreeError::AlreadyExists);
    }

    let usb = UsbBusDir::new("usb");
    let devices = UsbBusDir::new("devices");
    usb.add_child(devices.clone() as Arc<dyn SysObj>)?;
    aster_device::register_legacy_bus_dir(usb as Arc<dyn SysObj>)
        .map_err(|_| SysTreeError::InternalError("the device model rejected /sys/bus/usb"))?;
    Ok(devices)
}

/// A directory without attributes (`usb`, `devices`).
#[derive(Debug)]
struct UsbBusDir {
    fields: AttrLessBranchNodeFields<dyn SysObj, Self>,
}

#[inherit_methods(from = "self.fields")]
impl UsbBusDir {
    fn new(name: &str) -> Arc<Self> {
        Arc::new_cyclic(|weak_self| Self {
            fields: AttrLessBranchNodeFields::new(SysStr::from(name.to_string()), weak_self.clone()),
        })
    }

    fn add_child(&self, new_child: Arc<dyn SysObj>) -> SysTreeResult<()>;
}

inherit_sys_branch_node!(UsbBusDir, fields, {
    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

/// One device directory with its attribute set.
#[derive(Debug)]
struct UsbDeviceNode {
    fields: BranchNodeFields<dyn SysObj, Self>,
    attrs: Vec<(&'static str, String)>,
}

#[inherit_methods(from = "self.fields")]
impl UsbDeviceNode {
    fn new(name: &str, attrs: Vec<(&'static str, String)>) -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        for (attr, _) in &attrs {
            builder.add(SysStr::from(*attr), SysPerms::DEFAULT_RO_ATTR_PERMS);
        }
        let attr_set = builder
            .build()
            .expect("failed to build the USB attribute set");

        Arc::new_cyclic(|weak_self| Self {
            fields: BranchNodeFields::new(
                SysStr::from(name.to_string()),
                attr_set,
                weak_self.clone(),
            ),
            attrs,
        })
    }
}

inherit_sys_branch_node!(UsbDeviceNode, fields, {
    fn read_attr_at(
        &self,
        name: &str,
        offset: usize,
        writer: &mut VmWriter,
    ) -> SysTreeResult<usize> {
        let Some((_, content)) = self.attrs.iter().find(|(attr, _)| *attr == name) else {
            return Err(SysTreeError::AttributeError);
        };

        let mut printer = VmPrinter::new_skip(writer, offset);
        write!(printer, "{}", content)?;
        Ok(printer.bytes_written())
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});
