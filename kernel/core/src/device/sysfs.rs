// SPDX-License-Identifier: MPL-2.0

//! Linux-compatible sysfs topology for character devices.

use aster_bigtcp::{
    iface::{IfaceStatsSnapshot, InterfaceFlags, InterfaceType},
    wire::EthernetAddress,
};
use aster_systree::{
    AttrLessBranchNodeFields, BranchNodeFields, Error as SysTreeError, NormalNodeFields,
    Result as SysTreeResult, SymlinkNodeFields, SysAttrSetBuilder, SysBranchNode, SysObj,
    SysPerms, SysStr,
    inherit_sys_branch_node, inherit_sys_leaf_node, inherit_sys_symlink_node,
};
use aster_util::printer::VmPrinter;
use inherit_methods_macro::inherit_methods;

use super::tty;
use crate::{net::iface::Iface, prelude::*};

const FRAMEBUFFER_SUBSYSTEM_TARGET: &str = "../../../../bus/platform";
/// The same link, one level deeper: the DRM nodes hang off `/sys/dev/char`.
const DRM_SUBSYSTEM_TARGET: &str = "../../../../bus/platform";

/// The virtio device id of a GPU device (virtio spec 5.7), formatted as Linux
/// writes it in the device uevent.
const VIRTIO_GPU_MODALIAS: &str = "virtio:d00000010";

/// The character nodes and identity of one DRM device. A display controller
/// and a separate render GPU must not share their `device/drm` listing.
struct DrmSysDevice<'a> {
    nodes: &'a [(&'static str, u32)],
    driver: &'static str,
    modalias: &'static str,
}

pub(super) fn init_in_first_process() -> Result<()> {
    // Both directories are created through the same entry points the later
    // contributors use, so a block device that publishes before device init
    // still ends up under the same nodes instead of creating a second copy.
    let class_node = class_node();
    add_class_children(&class_node)?;

    let module_node = build_module_node()?;
    crate::fs::sysfs::systree_singleton()
        .root()
        .add_child(module_node as Arc<dyn SysObj>)?;

    let bus_node = AttrLessSysNode::new("bus");
    bus_node.add_child(AttrLessSysNode::new("platform") as Arc<dyn SysObj>)?;
    crate::fs::sysfs::systree_singleton()
        .root()
        .add_child(bus_node as Arc<dyn SysObj>)?;

    let drm_device = DrmSysDevice {
        nodes: super::dri::exposed_nodes(),
        driver: super::dri::driver_name(),
        modalias: VIRTIO_GPU_MODALIAS,
    };
    // Always present: `/sys/dev/block` is published by the block devices, and
    // `/sys/dev/char` hangs off the same directory when a DRM node exists.
    let dev_node = dev_node();
    if let Some(char_node) = build_char_node(&[drm_device])? {
        dev_node.add_child(char_node as Arc<dyn SysObj>)?;
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
fn build_char_node(devices: &[DrmSysDevice<'_>]) -> SysTreeResult<Option<Arc<AttrLessSysNode>>> {
    if devices.iter().all(|device| device.nodes.is_empty()) {
        return Ok(None);
    }

    let char_node = AttrLessSysNode::new("char");

    for drm_device in devices {
        for (node_name, minor) in drm_device.nodes {
            // The node's own `uevent`, which is a different file from the one
            // inside `device/` below and has a different consumer. See
            // `char_node_uevent`.
            let node = DevSysNode::new(
                &alloc::format!("{}:{}", super::dri::DRM_MAJOR, minor),
                &char_node_uevent(node_name, *minor),
            );

            // The device the node belongs to. Linux reaches it through a symlink
            // into `/sys/devices`; the traversable shape is what libdrm uses, and
            // it does not care whether the link is real.
            let device_node = DevSysNode::new(
                "device",
                &drm_uevent(drm_device.driver, drm_device.modalias),
            );
            device_node.add_child(
                SysfsSymlink::new("subsystem", DRM_SUBSYSTEM_TARGET) as Arc<dyn SysObj>
            )?;

            // Every node of the device, not just this one.
            //
            // This directory belongs to the *device*, not to the character node.
            // On Linux every `/sys/dev/char/<major>:<minor>/device/drm` for the
            // same device lists the same entries, because `device` is one symlink
            // they all resolve through. libdrm relies on that:
            // `drmGetDeviceNameFromFd2()` answers "which card node is this fd?" by
            // opening this directory and taking the first entry whose name starts
            // with `card`, so a render-node fd can only find its card node if the
            // render node's own listing contains it. Listing only the node that
            // was asked about leaves `card*` absent for `renderD128`, so the call
            // returns NULL, glamor turns that into `open(NULL, O_RDWR|O_CLOEXEC)`,
            // that fails with EFAULT, and `glamor_dri3_open_client` -- whose only
            // BadAlloc is that open -- answers every `DRI3Open` with BadAlloc. A
            // client that never receives a DRM fd never renders on the GPU.
            //
            // Emitting the whole device here is also what makes the two nodes
            // agree, so the answer does not depend on which fd the caller held.
            let drm_node = AttrLessSysNode::new("drm");
            for (name, _) in drm_device.nodes {
                drm_node.add_child(AttrLessSysNode::new(name) as Arc<dyn SysObj>)?;
            }
            device_node.add_child(drm_node as Arc<dyn SysObj>)?;

            node.add_child(device_node as Arc<dyn SysObj>)?;
            char_node.add_child(node as Arc<dyn SysObj>)?;
        }
    }

    Ok(Some(char_node))
}

/// The `uevent` contents of one DRM device.
///
/// Linux writes this file from the driver core, which has no counterpart here,
/// so the DRM driver supplies what it knows. `MODALIAS` carries the weight:
/// libdrm reads `OF_FULLNAME` and `OF_COMPATIBLE_0` first and falls back to
/// everything *after the last colon* in `MODALIAS` for both, so the owner of
/// each device supplies its own driver and bus identity.
fn drm_uevent(driver: &str, modalias: &str) -> String {
    alloc::format!("DRIVER={}\nMODALIAS={}\n", driver, modalias)
}

/// The `uevent` of a character device node itself.
///
/// This is not the file [`drm_uevent`] writes. That one lives at
/// `/sys/dev/char/<major>:<minor>/device/uevent` and describes the *device* --
/// bus, identifiers, modalias -- which is what `drmGetDevice2()` parses. This
/// one lives at `/sys/dev/char/<major>:<minor>/uevent` and describes the
/// *node*, and `drmGetDeviceNameFromFd2()` is its consumer: it opens the file,
/// looks for the line beginning `DEVNAME=`, and answers `/dev/<the rest>`. That
/// is the only way a caller holding a DRM file descriptor can turn it back into
/// a path.
///
/// Both are needed and neither implies the other, which is exactly how this was
/// missed. `device/uevent` was written, the node looked complete, and nothing
/// asked for the node's own file for several rounds -- until a GL client asked
/// the X server for a device fd and glamor, holding no name, could only call
/// `open(NULL, O_RDWR|O_CLOEXEC)` and return `BadAlloc`.
///
/// The fields are Linux's, in Linux's order: `MAJOR`, `MINOR`, `DEVNAME`,
/// `DEVTYPE`. `DEVNAME` is relative to `/dev`, so `dri/card0` and not
/// `/dev/dri/card0`.
fn char_node_uevent(node_name: &str, minor: u32) -> String {
    alloc::format!(
        "MAJOR={}\nMINOR={}\nDEVNAME=dri/{}\nDEVTYPE=drm_minor\n",
        super::dri::DRM_MAJOR,
        minor,
        node_name
    )
}

fn add_class_children(class_node: &Arc<AttrLessSysNode>) -> SysTreeResult<()> {
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

    class_node.add_child(build_net_class_node()?)?;

    Ok(())
}

// The network interface class -------------------------------------------------
//
// `/sys/class/net` is how user space finds interfaces without going through
// netlink: `ifconfig`, `iw`, `NetworkManager`, and most porting scripts walk
// this directory. The attributes below are the subset those tools open for a
// wired interface. Counters that the driver does not track are present and
// read as zero, because a missing file is an error while a zero is an answer.

const STATISTICS_ATTRS: [&str; 16] = [
    "rx_bytes",
    "rx_packets",
    "rx_errors",
    "rx_dropped",
    "rx_fifo_errors",
    "rx_frame_errors",
    "rx_compressed",
    "rx_multicast",
    "tx_bytes",
    "tx_packets",
    "tx_errors",
    "tx_dropped",
    "tx_fifo_errors",
    "tx_colls",
    "tx_carrier_errors",
    "tx_compressed",
];

const NET_IFACE_ATTRS: [&str; 16] = [
    "address",
    "addr_len",
    "broadcast",
    "type",
    "ifindex",
    "flags",
    "mtu",
    "operstate",
    "carrier",
    "dormant",
    "speed",
    "duplex",
    "tx_queue_len",
    "qdisc",
    "iflink",
    "dev_id",
];

fn build_net_class_node() -> SysTreeResult<Arc<AttrLessSysNode>> {
    let net_class_node = AttrLessSysNode::new("net");
    let ifaces = crate::net::iface::try_iter_all_ifaces()
        .into_iter()
        .flatten();
    for iface in ifaces {
        let node = NetIfaceSysNode::new(iface.clone());
        net_class_node.add_child(node as Arc<dyn SysObj>)?;
    }
    Ok(net_class_node)
}

/// `/sys/class/net/<name>`: the interface attributes and its `statistics/`
/// directory.
struct NetIfaceSysNode {
    fields: BranchNodeFields<dyn SysObj, Self>,
    iface: Arc<Iface>,
}

impl Debug for NetIfaceSysNode {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        formatter
            .debug_struct("NetIfaceSysNode")
            .field("iface", &self.iface.name())
            .finish_non_exhaustive()
    }
}

#[inherit_methods(from = "self.fields")]
impl NetIfaceSysNode {
    fn new(iface: Arc<Iface>) -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        for name in NET_IFACE_ATTRS {
            builder.add(SysStr::from(name), SysPerms::DEFAULT_RO_ATTR_PERMS);
        }
        let attrs = builder
            .build()
            .expect("failed to build the network interface attribute set");

        let name = iface.name().to_str().unwrap_or("net").to_string();
        let node = Arc::new_cyclic(|weak_self| Self {
            fields: BranchNodeFields::new(SysStr::from(name), attrs, weak_self.clone()),
            iface,
        });
        node.add_child(NetStatsSysNode::new(node.iface.clone()) as Arc<dyn SysObj>)
            .expect("failed to register the network statistics node");
        node
    }

    fn add_child(&self, new_child: Arc<dyn SysObj>) -> SysTreeResult<()>;
}

inherit_sys_branch_node!(NetIfaceSysNode, fields, {
    fn read_attr_at(
        &self,
        name: &str,
        offset: usize,
        writer: &mut VmWriter,
    ) -> SysTreeResult<usize> {
        let iface = &self.iface;
        let typ = iface.type_();
        let flags = iface.flags();
        let mut printer = VmPrinter::new_skip(writer, offset);
        match name {
            "address" => writeln!(printer, "{}", mac_text(iface.ethernet_addr()))?,
            "addr_len" => writeln!(printer, "6")?,
            "broadcast" => {
                let broadcast = if typ == InterfaceType::ETHER {
                    mac_text(Some(EthernetAddress::BROADCAST))
                } else {
                    mac_text(None)
                };
                writeln!(printer, "{}", broadcast)?
            }
            "type" => writeln!(printer, "{}", typ as u16)?,
            "ifindex" => writeln!(printer, "{}", iface.index())?,
            "flags" => writeln!(printer, "0x{:x}", netdev_flags(flags))?,
            "mtu" => writeln!(printer, "{}", iface.mtu())?,
            "operstate" => writeln!(printer, "{}", operstate(typ, flags))?,
            "carrier" => {
                let carrier = typ == InterfaceType::LOOPBACK
                    || flags.contains(InterfaceFlags::LOWER_UP)
                    || flags.contains(InterfaceFlags::RUNNING);
                writeln!(printer, "{}", carrier as u8)?
            }
            "dormant" => writeln!(printer, "0")?,
            // The Megrez link is a gigabit PHY; the driver does not negotiate
            // down and there is no speed-change event to report.
            "speed" => writeln!(printer, "1000")?,
            "duplex" => writeln!(printer, "full")?,
            "tx_queue_len" => writeln!(printer, "1000")?,
            "qdisc" => writeln!(
                printer,
                "{}",
                if typ == InterfaceType::LOOPBACK {
                    "noqueue"
                } else {
                    "mq"
                }
            )?,
            "iflink" => writeln!(printer, "{}", iface.index())?,
            "dev_id" => writeln!(printer, "0x0")?,
            _ => return Err(SysTreeError::AttributeError),
        }
        Ok(printer.bytes_written())
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

/// `/sys/class/net/<name>/statistics/`.
struct NetStatsSysNode {
    fields: BranchNodeFields<dyn SysObj, Self>,
    iface: Arc<Iface>,
}

impl Debug for NetStatsSysNode {
    fn fmt(&self, formatter: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        formatter
            .debug_struct("NetStatsSysNode")
            .field("iface", &self.iface.name())
            .finish_non_exhaustive()
    }
}

impl NetStatsSysNode {
    fn new(iface: Arc<Iface>) -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        for name in STATISTICS_ATTRS {
            builder.add(SysStr::from(name), SysPerms::DEFAULT_RO_ATTR_PERMS);
        }
        let attrs = builder
            .build()
            .expect("failed to build the network statistics attribute set");

        Arc::new_cyclic(|weak_self| Self {
            fields: BranchNodeFields::new(SysStr::from("statistics"), attrs, weak_self.clone()),
            iface,
        })
    }
}

inherit_sys_branch_node!(NetStatsSysNode, fields, {
    fn read_attr_at(
        &self,
        name: &str,
        offset: usize,
        writer: &mut VmWriter,
    ) -> SysTreeResult<usize> {
        let value =
            statistics_value(&self.iface.stats(), name).ok_or(SysTreeError::AttributeError)?;
        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(printer, "{}", value)?;
        Ok(printer.bytes_written())
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

/// Writes an Ethernet address the way sysfs does, and an all-zero address for
/// an interface that has none (loopback).
fn mac_text(addr: Option<EthernetAddress>) -> String {
    let bytes = addr.map(|addr| addr.0).unwrap_or([0u8; 6]);
    alloc::format!(
        "{:02x}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x}",
        bytes[0],
        bytes[1],
        bytes[2],
        bytes[3],
        bytes[4],
        bytes[5]
    )
}

/// The flag word `ifconfig` and `ip` expect in `/sys/class/net/<name>/flags`.
///
/// The internal flags carry three bits Linux does not expose through this
/// file: `IFF_RUNNING` (operational state, reported by `operstate` instead),
/// `IFF_LOWER_UP` and `IFF_ECHO`. Leaving them in makes a script that masks
/// for `IFF_UP` still work, but a script that compares the whole word to
/// Linux's value does not, so they are removed here.
fn netdev_flags(flags: InterfaceFlags) -> u32 {
    let hidden = InterfaceFlags::RUNNING.bits()
        | InterfaceFlags::LOWER_UP.bits()
        | InterfaceFlags::DORMANT.bits()
        | InterfaceFlags::ECHO.bits();
    flags.bits() & !hidden & 0xffff
}

fn operstate(typ: InterfaceType, flags: InterfaceFlags) -> &'static str {
    if typ == InterfaceType::LOOPBACK {
        return "unknown";
    }
    if !flags.contains(InterfaceFlags::UP) {
        return "down";
    }
    if flags.contains(InterfaceFlags::LOWER_UP) || flags.contains(InterfaceFlags::RUNNING) {
        "up"
    } else {
        "unknown"
    }
}

fn statistics_value(stats: &IfaceStatsSnapshot, name: &str) -> Option<u64> {
    let value = match name {
        "rx_bytes" => stats.rx_bytes,
        "rx_packets" => stats.rx_packets,
        "tx_bytes" => stats.tx_bytes,
        "tx_packets" => stats.tx_packets,
        // Only packets and bytes are counted by the interface today. The rest
        // exist so that readers see zero rather than a missing file.
        _ if STATISTICS_ATTRS.contains(&name) => 0,
        _ => return None,
    };
    Some(value)
}

pub(super) fn active_vt_attr_value(index: u32) -> String {
    alloc::format!("tty{}\n", index)
}


/// Handles on the two directories that several subsystems contribute to.
///
/// `/sys/class` is filled at init and `/sys/dev` is where the DRM nodes put
/// `char`; block devices add `block` under both, possibly long after init. The
/// tree only lets a concrete node add children, so the handles are kept here
/// instead of being looked up by name later.
static CLASS_NODE: spin::Once<Arc<AttrLessSysNode>> = spin::Once::new();
static DEV_NODE: spin::Once<Arc<AttrLessSysNode>> = spin::Once::new();

pub(super) fn class_node() -> Arc<AttrLessSysNode> {
    CLASS_NODE
        .call_once(|| {
            let node = AttrLessSysNode::new("class");
            crate::fs::sysfs::systree_singleton()
                .root()
                .add_child(node.clone() as Arc<dyn SysObj>)
                .expect("failed to create /sys/class");
            node
        })
        .clone()
}

pub(super) fn dev_node() -> Arc<AttrLessSysNode> {
    DEV_NODE
        .call_once(|| {
            let node = AttrLessSysNode::new("dev");
            crate::fs::sysfs::systree_singleton()
                .root()
                .add_child(node.clone() as Arc<dyn SysObj>)
                .expect("failed to create /sys/dev");
            node
        })
        .clone()
}

/// `/sys/module`: one directory per component that initialized.
///
/// There are no loadable modules in this kernel, so `/sys/module` is a view of
/// the component system rather than of a module loader. Tools read it to learn
/// whether a driver is present (`ls /sys/module | grep mmc`) and, through
/// `initstate`, whether it came up.
pub(super) fn build_module_node() -> SysTreeResult<Arc<AttrLessSysNode>> {
    let module_node = AttrLessSysNode::new("module");
    for name in component::initialized_components() {
        module_node.add_child(ModuleSysNode::new(name) as Arc<dyn SysObj>)?;
    }
    Ok(module_node)
}

/// One `/sys/module/<name>` directory.
#[derive(Debug)]
struct ModuleSysNode {
    fields: NormalNodeFields<Self>,
}

impl ModuleSysNode {
    fn new(name: String) -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        builder.add(SysStr::from("initstate"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        builder.add(SysStr::from("refcnt"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        let attrs = builder
            .build()
            .expect("failed to build the module attribute set");
        Arc::new_cyclic(|weak_self| Self {
            fields: NormalNodeFields::new(SysStr::Owned(name.clone()), attrs, weak_self.clone()),
        })
    }
}

inherit_sys_leaf_node!(ModuleSysNode, fields, {
    fn read_attr_at(
        &self,
        name: &str,
        offset: usize,
        writer: &mut VmWriter,
    ) -> SysTreeResult<usize> {
        // Everything listed here finished initializing, so the state is
        // `live`. `refcnt` is the module loader's reference count, which has
        // no meaning without a loader: report zero, as Linux does for a
        // module nothing holds.
        let value = match name {
            "initstate" => "live",
            "refcnt" => "0",
            _ => return Err(SysTreeError::AttributeError),
        };
        let mut printer = VmPrinter::new_skip(writer, offset);
        writeln!(printer, "{}", value)?;
        Ok(printer.bytes_written())
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

/// `/sys/devices`, the container the CPU topology hangs off.
static DEVICES_NODE: spin::Once<Arc<AttrLessSysNode>> = spin::Once::new();

pub(super) fn devices_node() -> Arc<AttrLessSysNode> {
    DEVICES_NODE
        .call_once(|| {
            let node = AttrLessSysNode::new("devices");
            crate::fs::sysfs::systree_singleton()
                .root()
                .add_child(node.clone() as Arc<dyn SysObj>)
                .expect("failed to create /sys/devices");
            node
        })
        .clone()
}

/// `/sys/class/block`, created on the first block device that needs it.
pub(super) fn block_class_node() -> Arc<AttrLessSysNode> {
    static BLOCK: spin::Once<Arc<AttrLessSysNode>> = spin::Once::new();
    BLOCK
        .call_once(|| {
            let node = AttrLessSysNode::new("block");
            class_node()
                .add_child(node.clone() as Arc<dyn SysObj>)
                .expect("failed to create /sys/class/block");
            node
        })
        .clone()
}

/// `/sys/dev/block`, created on the first block device that needs it.
pub(super) fn dev_block_node() -> Arc<AttrLessSysNode> {
    static BLOCK: spin::Once<Arc<AttrLessSysNode>> = spin::Once::new();
    BLOCK
        .call_once(|| {
            let node = AttrLessSysNode::new("block");
            dev_node()
                .add_child(node.clone() as Arc<dyn SysObj>)
                .expect("failed to create /sys/dev/block");
            node
        })
        .clone()
}

#[derive(Debug)]
pub(super) struct AttrLessSysNode {
    fields: AttrLessBranchNodeFields<dyn SysObj, Self>,
}

#[inherit_methods(from = "self.fields")]
impl AttrLessSysNode {
    pub(super) fn new(name: &str) -> Arc<Self> {
        Arc::new_cyclic(|weak_self| Self {
            fields: AttrLessBranchNodeFields::new(name.to_string().into(), weak_self.clone()),
        })
    }

    pub(super) fn add_child(&self, new_child: Arc<dyn SysObj>) -> SysTreeResult<()>;
}

inherit_sys_branch_node!(AttrLessSysNode, fields, {
    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

#[derive(Debug)]
pub(super) struct SysfsSymlink {
    fields: SymlinkNodeFields<Self>,
}

impl SysfsSymlink {
    pub(super) fn new(name: &str, target: &str) -> Arc<Self> {
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

    fn virtio_device<'a>(nodes: &'a [(&'static str, u32)]) -> DrmSysDevice<'a> {
        DrmSysDevice {
            nodes,
            driver: "virtio_gpu",
            modalias: VIRTIO_GPU_MODALIAS,
        }
    }

    /// Builds a detached `/sys/dev` node the way `init_in_first_process` does.
    ///
    /// The production path hangs these off the global systree singleton
    /// through `dev_node()`; a test wants the same shape without publishing it
    /// to a tree that other tests share.
    fn build_dev_node(devices: &[DrmSysDevice<'_>]) -> SysTreeResult<Arc<AttrLessSysNode>> {
        let dev = AttrLessSysNode::new("dev");
        if let Some(char_node) = build_char_node(devices)? {
            dev.add_child(char_node as Arc<dyn SysObj>)?;
        }
        Ok(dev)
    }

    /// The detached counterpart of `class_node()` + `add_class_children()`.
    fn build_class_node() -> SysTreeResult<Arc<AttrLessSysNode>> {
        let class = AttrLessSysNode::new("class");
        add_class_children(&class)?;
        Ok(class)
    }

    /// The shape libdrm walks to decide that `/dev/dri/card0` is a real
    /// device: a bus symlink it can readlink, a `uevent` it can read an
    /// identifier out of, and a `drm` directory it can list for the node name.
    #[ktest]
    fn drm_dev_node_matches_the_shape_libdrm_walks() {
        let dev = build_dev_node(&[virtio_device(&[("card0", 0), ("renderD128", 128)])]).unwrap();
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
        assert!(device.node_attrs().contains("uevent"));

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
        // The assertion above is the one that was here, and it passed while the
        // driver was broken: a node listing *itself* is exactly what the old
        // code did, so comparing the tree against its own shape could never
        // fail. What libdrm actually needs is the cross entry -- it is handed
        // whichever fd glamor holds (a render node) and has to name the card
        // node from it, by finding a `card*` entry in this directory. Assert
        // that, because that is the contract, and it is not a property the
        // node has by itself.
        assert!(render.child("card0").is_some());
        assert!(drm.child("renderD128").is_some());

        // The node's own `uevent` -- a different file from the `device/uevent`
        // asserted above, with a different reader. `drmGetDevice2()` parses the
        // one under `device/`; `drmGetDeviceNameFromFd2()` opens *this* one and
        // looks for `DEVNAME=`. The test asserted only the first for several
        // rounds while the second did not exist at all, and no GL client could
        // be given a device fd. Assert both, because neither implies the other.
        // Read it, rather than only checking that an attribute of that name is
        // present: `show_attr` is what libdrm's `fopen`+`read` reaches, so this
        // asserts the whole path from the tree to the bytes -- and the bytes
        // are the ones `drmGetDeviceNameFromFd2()` greps for `DEVNAME=`.
        let card_node = char.child("226:0").unwrap().cast_to_branch().unwrap();
        assert!(
            card_node
                .show_attr("uevent")
                .unwrap()
                .contains("DEVNAME=dri/card0\n")
        );
    }

    /// The display controller and GPU renderer are distinct devices on
    /// Megrez. A render fd must name its own card, not the display card.
    #[ktest]
    fn separate_drm_devices_do_not_share_their_node_lists() {
        let display_device = DrmSysDevice {
            nodes: &[("card0", 0)],
            driver: "simpledrm",
            modalias: "platform:simple-framebuffer",
        };
        let gpu_device = DrmSysDevice {
            nodes: &[("card1", 1), ("renderD128", 128)],
            driver: "pvrsrvkm",
            modalias: "platform:pvrsrvkm",
        };
        let dev = build_dev_node(&[display_device, gpu_device]).unwrap();
        let char = dev.child("char").unwrap().cast_to_branch().unwrap();
        let display = char
            .child("226:0")
            .unwrap()
            .cast_to_branch()
            .unwrap()
            .child("device")
            .unwrap()
            .cast_to_branch()
            .unwrap();
        let render = char
            .child("226:128")
            .unwrap()
            .cast_to_branch()
            .unwrap()
            .child("device")
            .unwrap()
            .cast_to_branch()
            .unwrap();

        let display_nodes = display.child("drm").unwrap().cast_to_branch().unwrap();
        let render_nodes = render.child("drm").unwrap().cast_to_branch().unwrap();
        assert!(display_nodes.child("card0").is_some());
        assert!(display_nodes.child("card1").is_none());
        assert!(render_nodes.child("card1").is_some());
        assert!(render_nodes.child("card0").is_none());
        assert!(
            display
                .show_attr("uevent")
                .unwrap()
                .contains("DRIVER=simpledrm\nMODALIAS=platform:simple-framebuffer\n")
        );
        assert!(
            render
                .show_attr("uevent")
                .unwrap()
                .contains("DRIVER=pvrsrvkm\nMODALIAS=platform:pvrsrvkm\n")
        );
    }

    /// The line `drmGetDeviceNameFromFd2()` searches for, and the form it has
    /// to be in: `DEVNAME` is relative to `/dev`, so `dri/card0` yields
    /// `/dev/dri/card0` and `/dev/dri/card0` would yield `/dev//dev/dri/card0`.
    #[ktest]
    fn char_node_uevent_names_the_node_the_way_libdrm_reads_it() {
        let card = char_node_uevent("card0", 0);
        assert!(card.contains("MAJOR=226\n"));
        assert!(card.contains("MINOR=0\n"));
        assert!(card.contains("DEVNAME=dri/card0\n"));

        let render = char_node_uevent("renderD128", 128);
        assert!(render.contains("MINOR=128\n"));
        assert!(render.contains("DEVNAME=dri/renderD128\n"));
    }

    /// libdrm reads `OF_FULLNAME`/`OF_COMPATIBLE_0` and falls back to the text
    /// after the last colon of `MODALIAS`, so the modalias has to be there and
    /// it has to contain a colon.
    #[ktest]
    fn drm_uevent_carries_a_modalias_libdrm_can_fall_back_to() {
        let uevent = drm_uevent("virtio_gpu", VIRTIO_GPU_MODALIAS);
        assert!(uevent.contains("MODALIAS=virtio:d00000010\n"));

        let modalias = uevent
            .lines()
            .find_map(|line| line.strip_prefix("MODALIAS="))
            .unwrap();
        assert_eq!(
            modalias.rsplit_once(':').map(|(_, rest)| rest),
            Some("d00000010")
        );
    }

    /// `/sys/dev` exists whenever sysfs does, because the block devices publish
    /// `/sys/dev/block` there; only the `char` subtree needs a DRM node.
    #[ktest]
    fn dev_node_exists_but_char_needs_a_device() {
        let empty = build_dev_node(&[]).unwrap();
        assert!(empty.child("char").is_none());
        assert_eq!(empty.children().len(), 0);

        let without_nodes = build_dev_node(&[virtio_device(&[])]).unwrap();
        assert!(without_nodes.child("char").is_none());

        let with_node = build_dev_node(&[virtio_device(&[("card0", 0)])]).unwrap();
        assert!(with_node.child("char").is_some());
    }

    #[ktest]
    fn net_class_reports_the_words_tools_parse() {
        assert_eq!(
            mac_text(Some(EthernetAddress::from_bytes(&[
                0x02, 0x4a, 0x1b, 0x00, 0xfe, 0x9d
            ]))),
            "02:4a:1b:00:fe:9d"
        );
        assert_eq!(mac_text(None), "00:00:00:00:00:00");

        // `up` and `lower_up` are internal; Linux's `flags` file carries
        // neither, so neither may leak into the word a script compares.
        let up = InterfaceFlags::UP
            | InterfaceFlags::BROADCAST
            | InterfaceFlags::MULTICAST
            | InterfaceFlags::RUNNING
            | InterfaceFlags::LOWER_UP;
        assert_eq!(netdev_flags(up), 0x1003);
        assert_eq!(
            netdev_flags(InterfaceFlags::UP | InterfaceFlags::LOOPBACK | InterfaceFlags::RUNNING),
            0x9
        );

        assert_eq!(operstate(InterfaceType::ETHER, up), "up");
        assert_eq!(
            operstate(InterfaceType::ETHER, InterfaceFlags::UP),
            "unknown"
        );
        assert_eq!(
            operstate(InterfaceType::ETHER, InterfaceFlags::empty()),
            "down"
        );
        assert_eq!(
            operstate(
                InterfaceType::LOOPBACK,
                InterfaceFlags::UP | InterfaceFlags::LOOPBACK
            ),
            "unknown"
        );

        let stats = IfaceStatsSnapshot {
            rx_bytes: 11,
            rx_packets: 12,
            tx_bytes: 13,
            tx_packets: 14,
        };
        assert_eq!(statistics_value(&stats, "rx_bytes"), Some(11));
        assert_eq!(statistics_value(&stats, "tx_packets"), Some(14));
        assert_eq!(statistics_value(&stats, "rx_errors"), Some(0));
        assert_eq!(statistics_value(&stats, "no_such_counter"), None);
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
