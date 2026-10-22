// SPDX-License-Identifier: MPL-2.0

//! Linux-compatible sysfs topology for DRM character devices.
//!
//! This is not decoration. libdrm answers two questions from
//! `/sys/dev/char/<major>:<minor>/` before it hands a DRM fd to Mesa:
//!
//! * `device/subsystem` says which bus the device is on, and `device/uevent`
//!   supplies the identifiers that go into `drmDevice`; without them
//!   `drmGetDevice2()` fails.
//! * `device/drm/` lists the card and render nodes of the device
//!   (`drmGetMinorType`), and the node's own `uevent` maps a file descriptor
//!   back to `/dev/dri/<name>` (`drmGetDeviceNameFromFd2`).
//!
//! When any of that is missing, Mesa cannot create its GBM device, EGL
//! reports `EGL_NOT_INITIALIZED`, glamor falls back and DRI3 never comes up —
//! while every DRM ioctl underneath keeps working.
//!
//! The device model ([`aster_device`]) owns `/sys/dev`, `/sys/class` and
//! `/sys/bus`. DRM has not been ported to it yet, so the subtree is built here
//! and attached through the model's legacy entry points, the same way
//! `register_legacy_virtual_node` bridges other subsystems.

use alloc::{format, string::ToString, sync::Arc};

use aster_systree::{
    AttrLessBranchNodeFields, BranchNodeFields, Error as SysTreeError, Result as SysTreeResult,
    SymlinkNodeFields, SysAttrSetBuilder, SysBranchNode, SysObj, SysPerms, SysStr,
    inherit_sys_branch_node, inherit_sys_symlink_node,
};
use aster_util::printer::VmPrinter;
use inherit_methods_macro::inherit_methods;

use crate::prelude::*;

/// `/sys/bus/platform`, the bus every virtio-mmio DRM device hangs off.
///
/// libdrm only reads the last component of the `subsystem` symlink, but a
/// dangling link would be a lie, so the directory is created as well.
const PLATFORM_BUS_TARGET: &str = "../../../../bus/platform";

/// Registers the `/sys/dev/char`, `/sys/class/drm` and `/sys/bus/platform`
/// view of one DRM device.
///
/// `nodes` lists the device's character nodes as `(name, minor)`, for example
/// `[("card0", 0), ("renderD128", 128)]`. Registration is best-effort from the
/// caller's point of view: a failure means libdrm cannot discover the device,
/// which the DRM core logs.
pub fn register_drm_device(
    major: u16,
    nodes: &[(&str, u32)],
    driver: &str,
    modalias: &str,
) -> Result<()> {
    if nodes.is_empty() {
        return Ok(());
    }

    let root = crate::fs::systree_singleton().root();
    let bus_root = root.child("bus");
    let class_root = root.child("class");
    let dev_root = root.child("dev");

    // /sys/bus/platform is shared by every platform device; create it once.
    if let Some(bus_root) = bus_root.as_ref()
        && !child_exists(bus_root, "platform")
    {
        let platform = AttrLessSysNode::new("platform");
        if let Err(error) =
            aster_device::register_legacy_bus_dir(platform as Arc<dyn SysObj>)
        {
            warn!("failed to register /sys/bus/platform: {:?}", error);
        }
    }

    // /sys/class/drm/<node>, one entry per node.
    if let Some(class_root) = class_root.as_ref()
        && !child_exists(class_root, "drm")
    {
        let drm_class = AttrLessSysNode::new("drm");
        for (name, minor) in nodes {
            let entry = DevSysNode::new(name, &class_uevent(major, name, *minor));
            if let Err(error) = drm_class.add_child(entry as Arc<dyn SysObj>) {
                warn!("failed to build /sys/class/drm/{}: {:?}", name, error);
            }
        }
        if let Err(error) =
            aster_device::register_legacy_class_dir(drm_class as Arc<dyn SysObj>)
        {
            warn!("failed to register /sys/class/drm: {:?}", error);
        }
    }

    // /sys/dev/char/<major>:<minor>, the node libdrm stat()s.
    if dev_root.is_none() {
        return Err(Error::with_message(
            Errno::EIO,
            "the device model has not created /sys/dev",
        ));
    }

    for (name, minor) in nodes {
        let node_name = format!("{major}:{minor}");
        let node = DevSysNode::new(&node_name, &char_node_uevent(major, name, *minor));

        // The device directory: bus identity plus the DRM node listing.
        let device_node = DevSysNode::new("device", &drm_uevent(driver, modalias));
        device_node.add_child(
            SysfsSymlink::new("subsystem", PLATFORM_BUS_TARGET) as Arc<dyn SysObj>
        )?;

        let drm_node = AttrLessSysNode::new("drm");
        for (node_name, _) in nodes {
            drm_node.add_child(AttrLessSysNode::new(node_name) as Arc<dyn SysObj>)?;
        }
        device_node.add_child(drm_node as Arc<dyn SysObj>)?;
        node.add_child(device_node as Arc<dyn SysObj>)?;

        aster_device::register_legacy_dev_entry(node as Arc<dyn SysObj>)
            .map_err(|error| Error::with_message(Errno::EIO, legacy_error_message(&error)))?;
    }

    Ok(())
}

fn legacy_error_message(error: &aster_device::common::Error) -> &'static str {
    use aster_device::common::Error as DeviceError;
    match error {
        DeviceError::NameConflict => "the DRM sysfs entry is already registered",
        _ => "failed to register the DRM sysfs nodes",
    }
}

fn child_exists(parent: &Arc<dyn SysObj>, name: &str) -> bool {
    parent
        .cast_to_branch()
        .map(|branch| branch.child(name).is_some())
        .unwrap_or(false)
}

/// The node's own `uevent`: `MAJOR`, `MINOR`, `DEVNAME`, `DEVTYPE`, in Linux's
/// order. `DEVNAME` is relative to `/dev`.
fn char_node_uevent(major: u16, node_name: &str, minor: u32) -> String {
    format!("MAJOR={major}\nMINOR={minor}\nDEVNAME=dri/{node_name}\nDEVTYPE=drm_minor\n")
}

/// The class entry's `uevent`: just the device number, as Linux prints it for
/// `/sys/class/drm/card0`.
fn class_uevent(major: u16, node_name: &str, minor: u32) -> String {
    format!("MAJOR={major}\nMINOR={minor}\nDEVNAME=dri/{node_name}\n")
}

/// The device's `uevent`: the driver and, when available, the modalias that
/// libdrm parses for bus identifiers.
fn drm_uevent(driver: &str, modalias: &str) -> String {
    if modalias.is_empty() {
        format!("DRIVER={driver}\n")
    } else {
        format!("DRIVER={driver}\nMODALIAS={modalias}\n")
    }
}

#[derive(Debug)]
struct AttrLessSysNode {
    fields: AttrLessBranchNodeFields<dyn SysObj, Self>,
}

#[inherit_methods(from = "self.fields")]
impl AttrLessSysNode {
    fn new(name: &str) -> Arc<Self> {
        Arc::new_cyclic(|weak_self| Self {
            fields: AttrLessBranchNodeFields::new(name.to_string().into(), weak_self.clone()),
        })
    }

    fn add_child(&self, new_child: Arc<dyn SysObj>) -> SysTreeResult<()>;
}

inherit_sys_branch_node!(AttrLessSysNode, fields, {
    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

#[derive(Debug)]
struct SysfsSymlink {
    fields: SymlinkNodeFields<Self>,
}

impl SysfsSymlink {
    fn new(name: &str, target: &str) -> Arc<Self> {
        Arc::new_cyclic(|weak_self| Self {
            fields: SymlinkNodeFields::new(
                SysStr::from(name.to_string()),
                target.to_string(),
                weak_self.clone(),
            ),
        })
    }
}

inherit_sys_symlink_node!(SysfsSymlink, fields);

/// A directory that also exposes one read-only attribute (`uevent`).
#[derive(Debug)]
struct DevSysNode {
    fields: BranchNodeFields<dyn SysObj, Self>,
    content: String,
}

#[inherit_methods(from = "self.fields")]
impl DevSysNode {
    fn new(name: &str, content: &str) -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        builder.add(SysStr::from("uevent"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        let attrs = builder
            .build()
            .expect("failed to build the uevent sysfs attribute set");

        Arc::new_cyclic(|weak_self| Self {
            fields: BranchNodeFields::new(SysStr::from(name.to_string()), attrs, weak_self.clone()),
            content: content.to_string(),
        })
    }

    fn add_child(&self, new_child: Arc<dyn SysObj>) -> SysTreeResult<()>;
}

inherit_sys_branch_node!(DevSysNode, fields, {
    fn read_attr_at(
        &self,
        name: &str,
        offset: usize,
        writer: &mut VmWriter,
    ) -> SysTreeResult<usize> {
        if name != "uevent" {
            return Err(SysTreeError::AttributeError);
        }

        let mut printer = VmPrinter::new_skip(writer, offset);
        write!(printer, "{}", self.content)?;
        Ok(printer.bytes_written())
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});
