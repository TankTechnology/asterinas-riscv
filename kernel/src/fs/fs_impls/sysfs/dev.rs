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
    AttrLessBranchNodeFields, BranchNodeFields, Error, Result, SysAttrSetBuilder, SysObj,
    SysPerms, SysStr, inherit_sys_branch_node,
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
/// attributes.
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

/// Registers `/sys/dev/char/226:0/device` for the DRM card0 device.
pub(super) fn init() {
    DEV_CHAR_ROOT.call_once(|| {
        let dev = AttrlessDir::new(SysStr::from("dev"));
        let char_dir = AttrlessDir::new(SysStr::from("char"));
        let card = AttrlessDir::new(SysStr::from("226:0"));
        let device = CharDeviceNode::new(SysStr::from("device"), 0x1af4, 0x1050);

        card.add_child(device).unwrap();
        char_dir.add_child(card).unwrap();
        dev.add_child(char_dir).unwrap();
        super::systree_singleton().root().add_child(dev).unwrap();
    });
}

static DEV_CHAR_ROOT: Once<()> = Once::new();
