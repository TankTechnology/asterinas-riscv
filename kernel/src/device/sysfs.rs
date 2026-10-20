// SPDX-License-Identifier: MPL-2.0

//! Linux-compatible sysfs topology for character devices.

use aster_systree::{
    AttrLessBranchNodeFields, BranchNodeFields, Error as SysTreeError, NormalNodeFields,
    Result as SysTreeResult, SymlinkNodeFields, SysAttrSetBuilder, SysObj, SysPerms, SysStr,
    inherit_sys_branch_node, inherit_sys_leaf_node, inherit_sys_symlink_node,
};
use aster_util::printer::VmPrinter;
use inherit_methods_macro::inherit_methods;

use super::tty;
use crate::prelude::*;

const FRAMEBUFFER_SUBSYSTEM_TARGET: &str = "../../../../bus/platform";

// The relative links below mirror Linux's virtual DRM topology.  libdrm uses
// these links (rather than just the character device) to identify the driver
// and to find the bus-specific device behind a render node.  Keep this tree
// read-only: node registration already happened in dri::init_in_first_kthread.
const DRM_CLASS_SUBSYSTEM_TARGET: &str = "../../../../../../class/drm";
const DRM_PARENT_DRIVER_TARGET: &str = "../../../../bus/virtio/drivers/virtio_gpu";
const DRM_PARENT_SUBSYSTEM_TARGET: &str = "../../../../bus/virtio";
const DRM_CLASS_NODE_TARGET: &str = "../../devices/virtual/virtio/virtio0/drm";
const DRM_CHAR_NODE_TARGET: &str = "../../devices/virtual/virtio/virtio0/drm";

pub(super) fn init_in_first_process() -> Result<()> {
    let class_node = build_class_node()?;
    let bus_node = AttrLessSysNode::new("bus");
    bus_node.add_child(AttrLessSysNode::new("platform") as Arc<dyn SysObj>)?;

    let root = crate::fs::sysfs::systree_singleton().root();
    root.add_child(class_node as Arc<dyn SysObj>)?;

    // A DRM node is created by dri::init_in_first_kthread before the first
    // process is made.  Expose the matching sysfs view only when that node is
    // really present; firmware-only machines must not claim a virtio device.
    if crate::device::dri::driver_name() == crate::device::dri::DRIVER_NAME
        && !crate::device::dri::exposed_nodes().is_empty()
    {
        let (devices, dev) = build_drm_nodes()?;
        let virtio_bus = AttrLessSysNode::new("virtio");
        let virtio_devices = AttrLessSysNode::new("devices");
        virtio_devices.add_child(SysfsSymlink::new(
            "virtio0",
            "../../../devices/virtual/virtio/virtio0",
        ) as Arc<dyn SysObj>)?;
        let virtio_drivers = AttrLessSysNode::new("drivers");
        virtio_drivers.add_child(AttrLessSysNode::new("virtio_gpu") as Arc<dyn SysObj>)?;
        virtio_bus.add_child(virtio_devices as Arc<dyn SysObj>)?;
        virtio_bus.add_child(virtio_drivers as Arc<dyn SysObj>)?;
        bus_node.add_child(virtio_bus as Arc<dyn SysObj>)?;
        let root = crate::fs::sysfs::systree_singleton().root();
        root.add_child(devices)?;
        root.add_child(dev)?;
    }
    root.add_child(bus_node as Arc<dyn SysObj>)?;
    Ok(())
}

fn build_class_node() -> SysTreeResult<Arc<AttrLessSysNode>> {
    let class_node = AttrLessSysNode::new("class");

    let tty_class_node = AttrLessSysNode::new("tty");
    tty_class_node.add_child(Tty0SysNode::new() as Arc<dyn SysObj>)?;
    class_node.add_child(tty_class_node as Arc<dyn SysObj>)?;

    let graphics_class_node = AttrLessSysNode::new("graphics");
    let framebuffer_node = AttrLessSysNode::new("fb0");
    let device_node = AttrLessSysNode::new("device");
    device_node.add_child(
        SysfsSymlink::new("subsystem", FRAMEBUFFER_SUBSYSTEM_TARGET) as Arc<dyn SysObj>
    )?;
    framebuffer_node.add_child(device_node as Arc<dyn SysObj>)?;
    graphics_class_node.add_child(framebuffer_node as Arc<dyn SysObj>)?;
    class_node.add_child(graphics_class_node as Arc<dyn SysObj>)?;

    if crate::device::dri::driver_name() == crate::device::dri::DRIVER_NAME
        && !crate::device::dri::exposed_nodes().is_empty()
    {
        class_node.add_child(build_drm_class_node()? as Arc<dyn SysObj>)?;
    }

    // The DRM class is attached after the root is built, because its device
    // symlinks refer to the virtual-device tree registered at the same time.

    Ok(class_node)
}

fn build_drm_nodes() -> SysTreeResult<(Arc<dyn SysObj>, Arc<dyn SysObj>)> {
    let devices = AttrLessSysNode::new("devices");
    let virtual_node = AttrLessSysNode::new("virtual");
    let virtio = DriVirtioBusSysNode::new();
    let parent = DriParentSysNode::new();
    let parent_drm = AttrLessSysNode::new("drm");
    // libdrm discovers the transport by reading the subsystem link on the
    // parent `device/drm` directory, reached from `/sys/dev/char/*/device`.
    // A link on each minor alone is insufficient: drmGetDevice2() opens
    // `/sys/dev/char/<major>:<minor>/device/drm/subsystem` before it looks at
    // the minor's own attributes.
    parent_drm
        .add_child(SysfsSymlink::new("subsystem", "../../../../../bus/virtio") as Arc<dyn SysObj>)?;
    for &(name, minor) in crate::device::dri::exposed_nodes() {
        parent_drm.add_child(DriMinorSysNode::new(name, minor) as Arc<dyn SysObj>)?;
    }
    parent.add_child(parent_drm as Arc<dyn SysObj>)?;
    virtio.add_child(parent as Arc<dyn SysObj>)?;
    virtual_node.add_child(virtio as Arc<dyn SysObj>)?;
    devices.add_child(virtual_node as Arc<dyn SysObj>)?;

    let dev = AttrLessSysNode::new("dev");
    let char_node = AttrLessSysNode::new("char");
    for &(name, minor) in crate::device::dri::exposed_nodes() {
        char_node.add_child(SysfsSymlink::new(
            &alloc::format!("{}:{}", crate::device::dri::DRM_MAJOR, minor),
            &alloc::format!("{DRM_CHAR_NODE_TARGET}/{name}"),
        ) as Arc<dyn SysObj>)?;
    }
    dev.add_child(char_node as Arc<dyn SysObj>)?;

    Ok((devices as Arc<dyn SysObj>, dev as Arc<dyn SysObj>))
}

fn build_drm_class_node() -> SysTreeResult<Arc<AttrLessSysNode>> {
    let drm_class = AttrLessSysNode::new("drm");
    for &(name, _) in crate::device::dri::exposed_nodes() {
        drm_class.add_child(SysfsSymlink::new(
            name,
            &alloc::format!("{DRM_CLASS_NODE_TARGET}/{name}"),
        ) as Arc<dyn SysObj>)?;
    }
    Ok(drm_class)
}

pub(super) fn active_vt_attr_value(index: u32) -> String {
    alloc::format!("tty{}\n", index)
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

/// A `/sys/class/drm/card0` or `/sys/class/drm/renderD128` node.
#[derive(Debug)]
struct DriMinorSysNode {
    fields: BranchNodeFields<dyn SysObj, Self>,
    minor: u32,
    node_name: &'static str,
}

#[inherit_methods(from = "self.fields")]
impl DriMinorSysNode {
    fn new(node_name: &'static str, minor: u32) -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        builder.add(SysStr::from("dev"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        builder.add(SysStr::from("uevent"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        let attrs = builder
            .build()
            .expect("failed to build the DRM minor attribute set");
        let node = Arc::new_cyclic(|weak_self| Self {
            fields: BranchNodeFields::new(SysStr::from(node_name), attrs, weak_self.clone()),
            minor,
            node_name,
        });
        node.add_child(
            SysfsSymlink::new("subsystem", DRM_CLASS_SUBSYSTEM_TARGET) as Arc<dyn SysObj>
        )
        .expect("failed to register the DRM subsystem symlink");
        node.add_child(SysfsSymlink::new("device", "../..") as Arc<dyn SysObj>)
            .expect("failed to register the DRM device symlink");
        node
    }

    fn add_child(&self, new_child: Arc<dyn SysObj>) -> SysTreeResult<()>;
}

inherit_sys_branch_node!(DriMinorSysNode, fields, {
    fn read_attr_at(
        &self,
        name: &str,
        offset: usize,
        writer: &mut VmWriter,
    ) -> SysTreeResult<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        match name {
            "dev" => writeln!(printer, "{}:{}", crate::device::dri::DRM_MAJOR, self.minor)?,
            "uevent" => {
                writeln!(printer, "MAJOR={}", crate::device::dri::DRM_MAJOR)?;
                writeln!(printer, "MINOR={}", self.minor)?;
                writeln!(printer, "DEVNAME=dri/{}", self.node_name)?;
                writeln!(printer, "DEVTYPE=drm_minor")?;
            }
            _ => return Err(SysTreeError::AttributeError),
        }
        Ok(printer.bytes_written())
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

/// The stable virtual parent behind the DRM nodes.  Mesa/libdrm follows this
/// parent to discover the virtio bus and the kernel driver.
#[derive(Debug)]
struct DriParentSysNode {
    fields: BranchNodeFields<dyn SysObj, Self>,
}

#[inherit_methods(from = "self.fields")]
impl DriParentSysNode {
    fn new() -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        builder.add(SysStr::from("uevent"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        for name in [
            "vendor",
            "device",
            "subsystem_vendor",
            "subsystem_device",
            "revision",
            "modalias",
        ] {
            builder.add(SysStr::from(name), SysPerms::DEFAULT_RO_ATTR_PERMS);
        }
        let attrs = builder
            .build()
            .expect("failed to build the DRM parent attribute set");
        let node = Arc::new_cyclic(|weak_self| Self {
            fields: BranchNodeFields::new(SysStr::from("virtio0"), attrs, weak_self.clone()),
        });
        node.add_child(SysfsSymlink::new("driver", DRM_PARENT_DRIVER_TARGET) as Arc<dyn SysObj>)
            .expect("failed to register the DRM driver symlink");
        node.add_child(
            SysfsSymlink::new("subsystem", DRM_PARENT_SUBSYSTEM_TARGET) as Arc<dyn SysObj>
        )
        .expect("failed to register the DRM parent subsystem symlink");
        node
    }

    fn add_child(&self, new_child: Arc<dyn SysObj>) -> SysTreeResult<()>;
}

inherit_sys_branch_node!(DriParentSysNode, fields, {
    fn read_attr_at(
        &self,
        name: &str,
        offset: usize,
        writer: &mut VmWriter,
    ) -> SysTreeResult<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        match name {
            "uevent" => {
                writeln!(printer, "DRIVER={}", crate::device::dri::driver_name())?;
                writeln!(printer, "DEVTYPE=pci_device")?;
                writeln!(printer, "PCI_SLOT_NAME=0000:00:00.0")?;
                writeln!(printer, "PCI_CLASS=030000")?;
                writeln!(printer, "PCI_ID=1AF4:1050")?;
                writeln!(printer, "PCI_SUBSYS_ID=1AF4:1100")?;
                writeln!(printer, "PCI_REVISION=01")?;
                writeln!(printer, "MODALIAS=virtio:d00000001v00001AF4")?;
            }
            // libdrm's virtio path intentionally reuses its PCI device
            // parser.  The QEMU virtio-mmio transport has no physical PCI
            // config space, so these stable IDs describe the guest's virtual
            // GPU and make the user-space discovery contract complete.
            "vendor" => writeln!(printer, "0x1af4")?,
            "device" => writeln!(printer, "0x1050")?,
            "subsystem_vendor" => writeln!(printer, "0x1af4")?,
            "subsystem_device" => writeln!(printer, "0x1100")?,
            "revision" => writeln!(printer, "0x01")?,
            "modalias" => writeln!(printer, "virtio:d00000001v00001AF4")?,
            _ => return Err(SysTreeError::AttributeError),
        }
        Ok(printer.bytes_written())
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

/// The libdrm virtio parser treats a virtio-mmio child like a PCI-compatible
/// device: after resolving `.../virtio0`, it strips the final `/virtio0` and
/// reads the PCI-shaped attributes from the parent `/virtual/virtio` node.
/// Linux's real virtio-pci tree naturally has those attributes on that
/// parent; publish the same read-only compatibility view for QEMU virtio-mmio
/// so `drmGetDevice2()` can identify the render node instead of returning
/// `ENODEV`.
#[derive(Debug)]
struct DriVirtioBusSysNode {
    fields: BranchNodeFields<dyn SysObj, Self>,
}

#[inherit_methods(from = "self.fields")]
impl DriVirtioBusSysNode {
    fn new() -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        builder.add(SysStr::from("uevent"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        for name in [
            "vendor",
            "device",
            "subsystem_vendor",
            "subsystem_device",
            "revision",
            "modalias",
        ] {
            builder.add(SysStr::from(name), SysPerms::DEFAULT_RO_ATTR_PERMS);
        }
        let attrs = builder
            .build()
            .expect("failed to build the DRM virtio bus attribute set");
        let node = Arc::new_cyclic(|weak_self| Self {
            fields: BranchNodeFields::new(SysStr::from("virtio"), attrs, weak_self.clone()),
        });
        node.add_child(SysfsSymlink::new("subsystem", "../../../bus/virtio") as Arc<dyn SysObj>)
            .expect("failed to register the DRM virtio bus subsystem symlink");
        node
    }

    fn add_child(&self, new_child: Arc<dyn SysObj>) -> SysTreeResult<()>;
}

inherit_sys_branch_node!(DriVirtioBusSysNode, fields, {
    fn read_attr_at(
        &self,
        name: &str,
        offset: usize,
        writer: &mut VmWriter,
    ) -> SysTreeResult<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        match name {
            "uevent" => {
                writeln!(printer, "PCI_SLOT_NAME=0000:00:00.0")?;
                writeln!(printer, "PCI_CLASS=030000")?;
                writeln!(printer, "PCI_ID=1AF4:1050")?;
                writeln!(printer, "PCI_SUBSYS_ID=1AF4:1100")?;
                writeln!(printer, "PCI_REVISION=01")?;
                writeln!(printer, "MODALIAS=virtio:d00000001v00001AF4")?;
            }
            "vendor" => writeln!(printer, "0x1af4")?,
            "device" => writeln!(printer, "0x1050")?,
            "subsystem_vendor" => writeln!(printer, "0x1af4")?,
            "subsystem_device" => writeln!(printer, "0x1100")?,
            "revision" => writeln!(printer, "0x01")?,
            "modalias" => writeln!(printer, "virtio:d00000001v00001AF4")?,
            _ => return Err(SysTreeError::AttributeError),
        }
        Ok(printer.bytes_written())
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

#[derive(Debug)]
struct Tty0SysNode {
    fields: NormalNodeFields<Self>,
}

impl Tty0SysNode {
    fn new() -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        builder.add(SysStr::from("active"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        let attrs = builder
            .build()
            .expect("failed to build the tty0 sysfs attribute set");

        Arc::new_cyclic(|weak_self| Self {
            fields: NormalNodeFields::new(SysStr::from("tty0"), attrs, weak_self.clone()),
        })
    }
}

inherit_sys_leaf_node!(Tty0SysNode, fields, {
    fn read_attr_at(
        &self,
        name: &str,
        offset: usize,
        writer: &mut VmWriter,
    ) -> SysTreeResult<usize> {
        if name != "active" {
            return Err(SysTreeError::AttributeError);
        }

        let mut printer = VmPrinter::new_skip(writer, offset);
        write!(printer, "{}", active_vt_attr_value(tty::active_vt_index()))?;
        Ok(printer.bytes_written())
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

#[cfg(ktest)]
mod test {
    use aster_systree::{SysBranchNode, SysSymlink};
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn framebuffer_subsystem_symlink_matches_linux_platform_topology() {
        let class = build_class_node().unwrap();
        let graphics = class.child("graphics").unwrap().cast_to_branch().unwrap();
        let fb0 = graphics.child("fb0").unwrap().cast_to_branch().unwrap();
        let device = fb0.child("device").unwrap().cast_to_branch().unwrap();
        let subsystem = device
            .child("subsystem")
            .unwrap()
            .cast_to_symlink()
            .unwrap();

        assert_eq!(subsystem.target_path(), FRAMEBUFFER_SUBSYSTEM_TARGET);
    }

    #[ktest]
    fn drm_symlink_topology_matches_libdrm_contract() {
        let minor = DriMinorSysNode::new("renderD128", 128);
        let device = minor.child("device").unwrap().cast_to_symlink().unwrap();
        let subsystem = minor.child("subsystem").unwrap().cast_to_symlink().unwrap();
        assert_eq!(device.target_path(), "../..");
        assert_eq!(subsystem.target_path(), DRM_CLASS_SUBSYSTEM_TARGET);

        let class_link = SysfsSymlink::new(
            "renderD128",
            &alloc::format!("{DRM_CLASS_NODE_TARGET}/renderD128"),
        );
        assert_eq!(
            class_link.target_path(),
            "../../devices/virtual/virtio/virtio0/drm/renderD128"
        );

        let char_link = SysfsSymlink::new(
            "226:128",
            &alloc::format!("{DRM_CHAR_NODE_TARGET}/renderD128"),
        );
        assert_eq!(
            char_link.target_path(),
            "../../devices/virtual/virtio/virtio0/drm/renderD128"
        );

        let parent = DriParentSysNode::new();
        assert_eq!(
            parent
                .child("subsystem")
                .unwrap()
                .cast_to_symlink()
                .unwrap()
                .target_path(),
            DRM_PARENT_SUBSYSTEM_TARGET
        );
        assert_eq!(
            parent
                .child("driver")
                .unwrap()
                .cast_to_symlink()
                .unwrap()
                .target_path(),
            DRM_PARENT_DRIVER_TARGET
        );

        let parent_drm = AttrLessSysNode::new("drm");
        parent_drm
            .add_child(
                SysfsSymlink::new("subsystem", "../../../../../bus/virtio") as Arc<dyn SysObj>
            )
            .unwrap();
        assert_eq!(
            parent_drm
                .child("subsystem")
                .unwrap()
                .cast_to_symlink()
                .unwrap()
                .target_path(),
            "../../../../../bus/virtio"
        );

        let devices = AttrLessSysNode::new("devices");
        devices
            .add_child(
                SysfsSymlink::new("virtio0", "../../../devices/virtual/virtio/virtio0")
                    as Arc<dyn SysObj>,
            )
            .unwrap();
        assert_eq!(
            devices
                .child("virtio0")
                .unwrap()
                .cast_to_symlink()
                .unwrap()
                .target_path(),
            "../../../devices/virtual/virtio/virtio0"
        );

        let bus = DriVirtioBusSysNode::new();
        assert_eq!(
            bus.child("subsystem")
                .unwrap()
                .cast_to_symlink()
                .unwrap()
                .target_path(),
            "../../../bus/virtio"
        );
    }
}
