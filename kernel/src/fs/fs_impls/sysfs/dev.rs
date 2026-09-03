// SPDX-License-Identifier: MPL-2.0

//! `/sys/dev/char` device tree for DRM devices.
//!
//! Mesa's DRI loader selects the GPU driver by reading the PCI vendor/device id from `/sys/dev/char/<major>:<minor>/device/{vendor,device}`.
//! Xorg also asks libdrm to reopen the device for DRI3 clients, which reads `DEVNAME` from `/sys/dev/char/<major>:<minor>/uevent`.
//! Virtio-gpu exposes both DRM nodes with PCI id `1af4:1050`.
//! Firmware framebuffer DRM exposes only a platform-backed primary node.

use alloc::sync::Arc;

use aster_systree::{
    AttrLessBranchNodeFields, BranchNodeFields, Error, Result, SymlinkNodeFields,
    SysAttrSetBuilder, SysObj, SysPerms, SysStr, inherit_sys_branch_node, inherit_sys_symlink_node,
};
use aster_util::printer::VmPrinter;
use inherit_methods_macro::inherit_methods;
use ostd::mm::{VmReader, VmWriter};
use spin::Once;

use crate::device::{DrmBackendKind, initialize_backend_kind};

const DRM_CHAR_MAJOR: u16 = 226;
const VIRTIO_PCI_VENDOR_ID: u32 = 0x1af4;
const VIRTIO_GPU_DEVICE_ID: u32 = 0x1050;
const VIRTIO_GPU_SUBSYSTEM_VENDOR_ID: u32 = 0x1af4;
const VIRTIO_GPU_SUBSYSTEM_DEVICE_ID: u32 = 0x1100;

/// An attribute-less directory in the sysfs tree.
#[derive(Debug)]
struct AttrlessDir {
    fields: AttrLessBranchNodeFields<dyn SysObj, Self>,
}

#[inherit_methods(from = "self.fields")]
impl AttrlessDir {
    fn new(name: SysStr) -> Arc<Self> {
        Arc::new_cyclic(|weak_self| {
            let fields = AttrLessBranchNodeFields::new(name, weak_self.clone());
            Self { fields }
        })
    }

    fn add_child(&self, new_child: Arc<dyn SysObj>) -> Result<()>;
}

inherit_sys_branch_node!(AttrlessDir, fields, {
    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

/// A `/sys/dev/char/226:*` DRM node with the `uevent` identity libdrm reads.
#[derive(Debug)]
struct DrmCharNode {
    fields: BranchNodeFields<dyn SysObj, Self>,
    minor: u16,
    dev_name: &'static str,
}

#[inherit_methods(from = "self.fields")]
impl DrmCharNode {
    fn new(minor: u16, dev_name: &'static str) -> Arc<Self> {
        let name = SysStr::from(alloc::format!("{}:{}", DRM_CHAR_MAJOR, minor));
        let mut builder = SysAttrSetBuilder::new();
        builder.add(SysStr::from("uevent"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        let attrs = builder
            .build()
            .expect("failed to build DRM char device sysfs attribute set");

        Arc::new_cyclic(|weak_self| {
            let fields = BranchNodeFields::new(name, attrs, weak_self.clone());
            Self {
                fields,
                minor,
                dev_name,
            }
        })
    }

    fn add_child(&self, new_child: Arc<dyn SysObj>) -> Result<()>;
}

inherit_sys_branch_node!(DrmCharNode, fields, {
    fn read_attr_at(&self, name: &str, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        if name != "uevent" {
            return Err(Error::AttributeError);
        }

        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(printer, "MAJOR={}", DRM_CHAR_MAJOR)?;
        writeln!(printer, "MINOR={}", self.minor)?;
        writeln!(printer, "DEVNAME={}", self.dev_name)?;
        writeln!(printer, "DEVTYPE=drm_minor")?;
        Ok(printer.bytes_written())
    }

    fn write_attr(&self, _name: &str, _reader: &mut VmReader) -> Result<usize> {
        Err(Error::AttributeError)
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

/// `/sys/dev/char/<major>:<minor>/device` with read-only `vendor`/`device`
/// attributes plus the extra fields libdrm's `drmGetDevice2` reads to decide
/// whether the device is render-capable: `subsystem_vendor`/`subsystem_device`
/// (hex, separate files) and a `uevent` carrying `PCI_SLOT_NAME`.
#[derive(Debug)]
struct VirtioGpuPciDeviceNode {
    fields: BranchNodeFields<dyn SysObj, Self>,
    vendor: u32,
    device: u32,
}

impl VirtioGpuPciDeviceNode {
    fn new(name: SysStr, vendor: u32, device: u32) -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        builder.add(SysStr::from("vendor"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        builder.add(SysStr::from("device"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        builder.add(
            SysStr::from("subsystem_vendor"),
            SysPerms::DEFAULT_RO_ATTR_PERMS,
        );
        builder.add(
            SysStr::from("subsystem_device"),
            SysPerms::DEFAULT_RO_ATTR_PERMS,
        );
        builder.add(SysStr::from("uevent"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        let attrs = builder
            .build()
            .expect("failed to build DRM device sysfs attribute set");

        Arc::new_cyclic(|weak_self| {
            let fields = BranchNodeFields::new(name, attrs, weak_self.clone());
            Self {
                fields,
                vendor,
                device,
            }
        })
    }
}

inherit_sys_branch_node!(VirtioGpuPciDeviceNode, fields, {
    fn read_attr_at(&self, name: &str, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        match name {
            "vendor" => {
                writeln!(printer, "0x{:04x}", self.vendor)?;
            }
            "device" => {
                writeln!(printer, "0x{:04x}", self.device)?;
            }
            "subsystem_vendor" => {
                writeln!(printer, "0x{:04x}", VIRTIO_GPU_SUBSYSTEM_VENDOR_ID)?;
            }
            "subsystem_device" => {
                writeln!(printer, "0x{:04x}", VIRTIO_GPU_SUBSYSTEM_DEVICE_ID)?;
            }
            "uevent" => {
                writeln!(printer, "DRIVER=virtio_gpu")?;
                writeln!(printer, "PCI_ID={:04X}:{:04X}", self.vendor, self.device)?;
                writeln!(
                    printer,
                    "PCI_SUBSYS_ID={:04X}:{:04X}",
                    VIRTIO_GPU_SUBSYSTEM_VENDOR_ID, VIRTIO_GPU_SUBSYSTEM_DEVICE_ID
                )?;
                writeln!(printer, "PCI_SLOT_NAME=0000:00:01.0")?;
            }
            _ => return Err(Error::AttributeError),
        }
        Ok(printer.bytes_written())
    }

    fn write_attr(&self, _name: &str, _reader: &mut VmReader) -> Result<usize> {
        Err(Error::AttributeError)
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

/// The `subsystem` symlink below each `/sys/dev/char/226:*/device` directory.
///
/// libdrm's `get_subsystem_type` reads this link and classifies the device by
/// the last component of the target (`/pci` => DRM_BUS_PCI).
#[derive(Debug)]
struct SubsystemSymlink {
    fields: SymlinkNodeFields<Self>,
}

impl SubsystemSymlink {
    fn new(name: SysStr, target: &str) -> Arc<Self> {
        Arc::new_cyclic(|weak_self| {
            let fields = SymlinkNodeFields::new(
                name,
                alloc::string::String::from(target),
                weak_self.clone(),
            );
            Self { fields }
        })
    }
}

inherit_sys_symlink_node!(SubsystemSymlink, fields);

fn add_virtio_drm_nodes(char_dir: &Arc<AttrlessDir>) {
    for (minor, dev_name) in [(0, "dri/card0"), (128, "dri/renderD128")] {
        let node = DrmCharNode::new(minor, dev_name);
        let device = VirtioGpuPciDeviceNode::new(
            SysStr::from("device"),
            VIRTIO_PCI_VENDOR_ID,
            VIRTIO_GPU_DEVICE_ID,
        );
        let subsystem = SubsystemSymlink::new(SysStr::from("subsystem"), "/sys/bus/pci");
        // libdrm's drmNodeIsDRM() stats this path to decide whether a
        // device is a DRM node; it just needs to exist.
        let drm_dir = AttrlessDir::new(SysStr::from("drm"));

        device.fields.add_child(subsystem).unwrap();
        device.fields.add_child(drm_dir).unwrap();
        node.add_child(device).unwrap();
        char_dir.add_child(node).unwrap();
    }
}

fn add_firmware_drm_node(char_dir: &Arc<AttrlessDir>) {
    let node = DrmCharNode::new(0, "dri/card0");
    let device = AttrlessDir::new(SysStr::from("device"));
    let subsystem = SubsystemSymlink::new(SysStr::from("subsystem"), "/sys/bus/platform");
    let drm_dir = AttrlessDir::new(SysStr::from("drm"));

    device.add_child(subsystem).unwrap();
    device.add_child(drm_dir).unwrap();
    node.add_child(device).unwrap();
    char_dir.add_child(node).unwrap();
}

/// Registers `/sys/dev/char/<major>:<minor>` for the available DRM backend.
pub(super) fn init() {
    DEV_CHAR_ROOT.call_once(|| {
        let dev = AttrlessDir::new(SysStr::from("dev"));
        let char_dir = AttrlessDir::new(SysStr::from("char"));

        match initialize_backend_kind() {
            Some(DrmBackendKind::Virtio) => add_virtio_drm_nodes(&char_dir),
            Some(DrmBackendKind::Firmware) => add_firmware_drm_node(&char_dir),
            None => {}
        }

        dev.add_child(char_dir).unwrap();
        super::systree_singleton().root().add_child(dev).unwrap();
    });
}

static DEV_CHAR_ROOT: Once<()> = Once::new();

#[cfg(ktest)]
mod tests {
    use aster_systree::{SysBranchNode, SysNode};
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn drm_char_uevent_reports_device_name() {
        let node = DrmCharNode::new(0, "dri/card0");
        let mut output = [0u8; 128];
        let mut writer = VmWriter::from(&mut output[..]).to_fallible();

        let bytes_written = node
            .read_attr_at("uevent", 0, &mut writer)
            .expect("DRM char uevent should be readable");
        let uevent = core::str::from_utf8(&output[..bytes_written])
            .expect("DRM char uevent should be UTF-8");

        assert!(uevent.contains("MAJOR=226\n"));
        assert!(uevent.contains("MINOR=0\n"));
        assert!(uevent.contains("DEVNAME=dri/card0\n"));
        assert!(uevent.contains("DEVTYPE=drm_minor\n"));
    }

    #[ktest]
    fn firmware_drm_has_only_a_platform_primary_node() {
        let char_dir = AttrlessDir::new(SysStr::from("char"));
        add_firmware_drm_node(&char_dir);

        let primary = char_dir.child("226:0").unwrap().cast_to_branch().unwrap();
        let device = primary.child("device").unwrap().cast_to_branch().unwrap();
        let subsystem = device
            .child("subsystem")
            .unwrap()
            .cast_to_symlink()
            .unwrap();

        assert_eq!(subsystem.target_path(), "/sys/bus/platform");
        assert!(device.child("drm").is_some());
        assert!(char_dir.child("226:128").is_none());
    }

    #[ktest]
    fn virtio_drm_keeps_primary_and_render_pci_nodes() {
        let char_dir = AttrlessDir::new(SysStr::from("char"));
        add_virtio_drm_nodes(&char_dir);

        for name in ["226:0", "226:128"] {
            let node = char_dir.child(name).unwrap().cast_to_branch().unwrap();
            let device = node.child("device").unwrap().cast_to_branch().unwrap();
            let subsystem = device
                .child("subsystem")
                .unwrap()
                .cast_to_symlink()
                .unwrap();
            assert_eq!(subsystem.target_path(), "/sys/bus/pci");
        }
    }
}
