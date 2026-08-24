// SPDX-License-Identifier: MPL-2.0

//! `/sys/dev/char` device tree for the DRM (virtio-gpu) device.
//!
//! Mesa's DRI loader selects the GPU driver by reading the PCI vendor/device
//! id from `/sys/dev/char/<major>:<minor>/device/{vendor,device}`. Without
//! these files the loader reports "failed to retrieve device information" and
//! silently falls back to the software `llvmpipe` driver, so virgl is never
//! selected. This module exposes that path for the DRM char device
//! (major 226, minor 0), which is backed by virtio-gpu (PCI id `1af4:1050`).

use alloc::sync::Arc;

use aster_systree::{
    AttrLessBranchNodeFields, BranchNodeFields, Error, Result, SymlinkNodeFields, SysAttrSetBuilder,
    SysObj, SysPerms, SysStr, inherit_sys_branch_node, inherit_sys_symlink_node,
};
use aster_util::printer::VmPrinter;
use inherit_methods_macro::inherit_methods;
use ostd::mm::{VmReader, VmWriter};
use spin::Once;

/// An attribute-less directory in the sysfs tree.
#[derive(Debug)]
pub struct AttrlessDir {
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

/// `/sys/dev/char/<major>:<minor>/device` with read-only `vendor`/`device`
/// attributes plus the extra fields libdrm's `drmGetDevice2` reads to decide
/// whether the device is render-capable: `subsystem_vendor`/`subsystem_device`
/// (hex, separate files) and a `uevent` carrying `PCI_SLOT_NAME`.
#[derive(Debug)]
pub struct CharDeviceNode {
    fields: BranchNodeFields<dyn SysObj, Self>,
    vendor: u32,
    device: u32,
}

impl CharDeviceNode {
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

inherit_sys_branch_node!(CharDeviceNode, fields, {
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
                // virtio vendor id
                writeln!(printer, "0x1af4")?;
            }
            "subsystem_device" => {
                writeln!(printer, "0x1100")?;
            }
            "uevent" => {
                writeln!(printer, "DRIVER=virtio_gpu")?;
                writeln!(printer, "PCI_ID=1AF4:1050")?;
                writeln!(printer, "PCI_SUBSYS_ID=1AF4:1100")?;
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

/// The `subsystem` symlink under `/sys/dev/char/226:0/device`. libdrm's
/// `get_subsystem_type` readlink's this path and classifies the device by the
/// last component of the target (`/pci` => DRM_BUS_PCI).
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

/// Registers `/sys/dev/char/<major>:<minor>/device` for both DRM nodes —
/// card0 (minor 0) and renderD128 (minor 128) — so libdrm's `drmGetDevice2`
/// enumeration sees a valid sysfs entry (and thus a render-capable device)
/// for each node.
pub(super) fn init() {
    DEV_CHAR_ROOT.call_once(|| {
        let dev = AttrlessDir::new(SysStr::from("dev"));
        let char_dir = AttrlessDir::new(SysStr::from("char"));

        for minor in ["226:0", "226:128"] {
            let node = AttrlessDir::new(SysStr::from(minor));
            let device = CharDeviceNode::new(SysStr::from("device"), 0x1af4, 0x1050);
            let subsystem = SubsystemSymlink::new(SysStr::from("subsystem"), "/sys/bus/pci");
            // libdrm's drmNodeIsDRM() stats this path to decide whether a
            // device is a DRM node; it just needs to exist.
            let drm_dir = AttrlessDir::new(SysStr::from("drm"));

            device.fields.add_child(subsystem).unwrap();
            device.fields.add_child(drm_dir).unwrap();
            node.add_child(device).unwrap();
            char_dir.add_child(node).unwrap();
        }

        dev.add_child(char_dir).unwrap();
        super::systree_singleton().root().add_child(dev).unwrap();
    });
}

static DEV_CHAR_ROOT: Once<()> = Once::new();
