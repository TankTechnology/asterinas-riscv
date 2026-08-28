// SPDX-License-Identifier: MPL-2.0

//! Linux-compatible sysfs topology for character devices.

use aster_systree::{
    AttrLessBranchNodeFields, Error as SysTreeError, NormalNodeFields, Result as SysTreeResult,
    SymlinkNodeFields, SysAttrSetBuilder, SysObj, SysPerms, SysStr, inherit_sys_branch_node,
    inherit_sys_leaf_node, inherit_sys_symlink_node,
};
use aster_util::printer::VmPrinter;
use inherit_methods_macro::inherit_methods;

use super::tty;
use crate::prelude::*;

const FRAMEBUFFER_SUBSYSTEM_TARGET: &str = "../../../../bus/platform";

pub(super) fn init_in_first_process() -> Result<()> {
    let class_node = build_class_node()?;
    let bus_node = AttrLessSysNode::new("bus");
    bus_node.add_child(AttrLessSysNode::new("platform") as Arc<dyn SysObj>)?;

    let root = crate::fs::sysfs::systree_singleton().root();
    root.add_child(class_node as Arc<dyn SysObj>)?;
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
