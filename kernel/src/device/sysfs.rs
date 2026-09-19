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
/// The same link, one level deeper: the DRM nodes hang off `/sys/dev/char`.
const DRM_SUBSYSTEM_TARGET: &str = "../../../../bus/platform";

/// The virtio device id of a GPU device (virtio spec 5.7).
const VIRTIO_ID_GPU: u32 = 16;

pub(super) fn init_in_first_process() -> Result<()> {
    let class_node = build_class_node()?;
    let bus_node = AttrLessSysNode::new("bus");
    bus_node.add_child(AttrLessSysNode::new("platform") as Arc<dyn SysObj>)?;

    let root = crate::fs::sysfs::systree_singleton().root();
    root.add_child(class_node as Arc<dyn SysObj>)?;
    root.add_child(bus_node as Arc<dyn SysObj>)?;
    if let Some(dev_node) = build_dev_node(super::dri::exposed_nodes())? {
        root.add_child(dev_node as Arc<dyn SysObj>)?;
    }
    Ok(())
}

/// Builds `/sys/dev/char/<major>:<minor>` for the DRM nodes, when there are
/// any.
///
/// This subtree is not decoration. libdrm asks two questions of it before it
/// will hand a device to Mesa, and both are answered nowhere else:
/// `readlink .../device/subsystem` says what bus the device is on, and
/// `.../device/uevent` supplies the identifiers that go into the `drmDevice`
/// structure. Without them `drmGetDevice2()` returns `ENOENT`, Mesa finds no
/// EGL device for `/dev/dri/card0`, and the GL stack settles on llvmpipe --
/// silently, and however well the DRM ioctls underneath it work.
///
/// `/sys/dev` is the one part of the device model that is keyed by the
/// character device's own major:minor rather than by a bus or class, which is
/// why it is built here and not from a class node.
fn build_dev_node(nodes: &[(&str, u32)]) -> SysTreeResult<Option<Arc<AttrLessSysNode>>> {
    if nodes.is_empty() {
        return Ok(None);
    }

    let dev_node = AttrLessSysNode::new("dev");
    let char_node = AttrLessSysNode::new("char");

    for (node_name, minor) in nodes {
        let node = AttrLessSysNode::new(&alloc::format!("{}:{}", super::dri::DRM_MAJOR, minor));

        // The device the node belongs to. Linux reaches it through a symlink
        // into `/sys/devices`; the traversable shape is what libdrm uses, and
        // it does not care whether the link is real.
        let device_node = DevSysNode::new("device", &drm_uevent());
        device_node.add_child(
            SysfsSymlink::new("subsystem", DRM_SUBSYSTEM_TARGET) as Arc<dyn SysObj>
        )?;

        // `drmGetPrimaryDeviceNameFromFd` opens this directory and returns
        // `/dev/dri/<first entry whose name starts with card or renderD>`.
        let drm_node = AttrLessSysNode::new("drm");
        drm_node.add_child(AttrLessSysNode::new(node_name) as Arc<dyn SysObj>)?;
        device_node.add_child(drm_node as Arc<dyn SysObj>)?;

        node.add_child(device_node as Arc<dyn SysObj>)?;
        char_node.add_child(node as Arc<dyn SysObj>)?;
    }

    dev_node.add_child(char_node as Arc<dyn SysObj>)?;
    Ok(Some(dev_node))
}

/// The `uevent` contents of a virtio-gpu DRM device.
///
/// Linux writes this file from the driver core, which has no counterpart here,
/// so the DRM driver supplies what it knows. `MODALIAS` carries the weight:
/// libdrm reads `OF_FULLNAME` and `OF_COMPATIBLE_0` first and falls back to
/// everything *after the last colon* in `MODALIAS` for both, so one line
/// answers both queries. `virtio:d%08X` of the device id is verbatim what
/// Linux's virtio bus writes there.
fn drm_uevent() -> String {
    alloc::format!(
        "DRIVER={}\nMODALIAS=virtio:d{:08X}\n",
        super::dri::DRIVER_NAME,
        VIRTIO_ID_GPU
    )
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

    Ok(class_node)
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

/// A device directory: attributes of its own, and children.
///
/// Linux sysfs device directories are both, and the DRM device's is the case
/// that matters here — libdrm opens `uevent` as a file *and* lists `drm/` and
/// readlinks `subsystem` in the same directory, so the node has to carry
/// attributes without giving up its children.
///
/// Linux generates `uevent` from the device model by asking each driver for
/// its identifiers. There is no device model to ask here, so the driver that
/// owns the device passes the text in.
#[derive(Debug)]
struct DevSysNode {
    fields: BranchNodeFields<dyn SysObj, Self>,
    uevent: String,
}

#[inherit_methods(from = "self.fields")]
impl DevSysNode {
    fn new(name: &str, uevent: &str) -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        builder.add(SysStr::from("uevent"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        let attrs = builder
            .build()
            .expect("failed to build the uevent sysfs attribute set");

        Arc::new_cyclic(|weak_self| Self {
            fields: BranchNodeFields::new(SysStr::from(name.to_string()), attrs, weak_self.clone()),
            uevent: uevent.to_string(),
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
        write!(printer, "{}", self.uevent)?;
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
    use aster_systree::SysBranchNode;
    use ostd::prelude::ktest;

    use super::*;

    /// The shape libdrm walks to decide that `/dev/dri/card0` is a real
    /// device: a bus symlink it can readlink, a `uevent` it can read an
    /// identifier out of, and a `drm` directory it can list for the node name.
    #[ktest]
    fn drm_dev_node_matches_the_shape_libdrm_walks() {
        let dev = build_dev_node(&[("card0", 0), ("renderD128", 128)])
            .unwrap()
            .unwrap();
        let char = dev.child("char").unwrap().cast_to_branch().unwrap();

        let card = char.child("226:0").unwrap().cast_to_branch().unwrap();
        let device = card.child("device").unwrap().cast_to_branch().unwrap();
        assert_eq!(
            device
                .child("subsystem")
                .unwrap()
                .cast_to_symlink()
                .unwrap()
                .target_path(),
            DRM_SUBSYSTEM_TARGET
        );
        // `uevent` has to be a readable attribute of the device directory
        // itself, not a child node, or libdrm's `fopen` on it finds a
        // directory and gives up.
        assert!(device.attributes().contains(&SysStr::from("uevent")));

        let drm = device.child("drm").unwrap().cast_to_branch().unwrap();
        assert!(drm.child("card0").is_some());

        let render = char
            .child("226:128")
            .unwrap()
            .cast_to_branch()
            .unwrap()
            .child("device")
            .unwrap()
            .cast_to_branch()
            .unwrap()
            .child("drm")
            .unwrap()
            .cast_to_branch()
            .unwrap();
        assert!(render.child("renderD128").is_some());
    }

    /// libdrm reads `OF_FULLNAME`/`OF_COMPATIBLE_0` and falls back to the text
    /// after the last colon of `MODALIAS`, so the modalias has to be there and
    /// it has to contain a colon.
    #[ktest]
    fn drm_uevent_carries_a_modalias_libdrm_can_fall_back_to() {
        let uevent = drm_uevent();
        assert!(uevent.contains("MODALIAS=virtio:d00000010\n"));

        let modalias = uevent
            .lines()
            .find_map(|line| line.strip_prefix("MODALIAS="))
            .unwrap();
        assert_eq!(modalias.rsplit_once(':').map(|(_, rest)| rest), Some("d00000010"));
    }

    /// Nothing to describe means nothing in the tree, rather than a device
    /// node for a device that is not there.
    #[ktest]
    fn drm_dev_node_is_absent_without_a_device() {
        assert!(build_dev_node(&[]).unwrap().is_none());
    }

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
}
