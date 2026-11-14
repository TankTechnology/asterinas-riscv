// SPDX-License-Identifier: MPL-2.0

//! The generic part of `/sys/block`.
//!
//! Every registered block device gets a directory under `/sys/block` with the
//! attributes `lsblk`, `blkid` and `udev`'s helpers open: `dev`, `size`, `ro`,
//! `removable`, `range`, `stat` and `inflight`. A partition is a child
//! directory of its whole device, with a `partition` index.
//!
//! The loop driver's nodes are *not* built here: loop devices have extra
//! attributes that only the loop driver knows, so it builds its own nodes and
//! shares this node as the parent.

use aster_block::BlockDevice;
use aster_systree::{
    AttrLessBranchNodeFields, BranchNodeFields, Error as SysTreeError, Result as SysTreeResult,
    SysAttrSetBuilder, SysObj, SysPerms, SysStr, inherit_sys_branch_node,
};
use aster_util::printer::VmPrinter;
use inherit_methods_macro::inherit_methods;
use spin::Once;

use crate::prelude::*;

/// The `/sys/block` branch node, shared by every block driver.
#[derive(Debug)]
pub(super) struct BlockSysNode {
    fields: AttrLessBranchNodeFields<dyn SysObj, Self>,
}

#[inherit_methods(from = "self.fields")]
impl BlockSysNode {
    fn new() -> Arc<Self> {
        Arc::new_cyclic(|weak_self| Self {
            fields: AttrLessBranchNodeFields::new(SysStr::from("block"), weak_self.clone()),
        })
    }

    /// Returns the one `/sys/block` node, creating and attaching it on first
    /// use.
    pub(super) fn get() -> Arc<Self> {
        static BLOCK_SYS_NODE: Once<Arc<BlockSysNode>> = Once::new();

        BLOCK_SYS_NODE
            .call_once(|| {
                let node = Self::new();
                // The sysfs tree is initialized before device init.
                crate::fs::sysfs::systree_singleton()
                    .root()
                    .add_child(node.clone() as Arc<dyn SysObj>)
                    .expect("failed to register the /sys/block sysfs node");
                node
            })
            .clone()
    }

    pub(super) fn add_child(&self, new_child: Arc<dyn SysObj>) -> SysTreeResult<()>;
}

inherit_sys_branch_node!(BlockSysNode, fields, {
    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

/// Adds the directories of every registered block device that is not a loop
/// device. Loop devices are added by their driver.
pub(super) fn add_registered_block_devices() {
    let block_node = BlockSysNode::get();
    for device in aster_block::collect_all() {
        if device.name().starts_with("loop") || device.is_partition() {
            continue;
        }
        let node = GenericBlockSysNode::new(device.clone());
        if let Err(error) = block_node.add_child(node.clone() as Arc<dyn SysObj>) {
            warn!(
                "failed to register /sys/block/{}: {:?}",
                device.name(),
                error
            );
            continue;
        }
        let target = alloc::format!("../../block/{}", device.name());
        if let Err(error) = publish_lookup_links(&device, &target) {
            warn!(
                "failed to publish lookups for /sys/block/{}: {:?}",
                device.name(),
                error
            );
        }
        for partition in device.partitions().unwrap_or_default() {
            let partition_node = GenericBlockSysNode::new(partition.clone());
            let target = alloc::format!(
                "../../block/{}/{}",
                device.name(),
                partition.name()
            );
            if let Err(error) = publish_lookup_links(&partition, &target) {
                warn!(
                    "failed to publish lookups for /sys/block/{}/{}: {:?}",
                    device.name(),
                    partition.name(),
                    error
                );
            }
            if let Err(error) = node.add_child(partition_node as Arc<dyn SysObj>) {
                warn!(
                    "failed to register /sys/block/{}/{}: {:?}",
                    device.name(),
                    partition.name(),
                    error
                );
            }
        }
    }
}


/// Publishes the two lookups user space uses to find a block device by name
/// and by number: `/sys/class/block/<name>` and `/sys/dev/block/<major>:<minor>`.
///
/// Both are symlinks into `/sys/block`, which stays the place the attributes
/// live. `lsblk` opens `/sys/dev/block` before it will list anything at all --
/// without the directory it exits with "failed to access sysfs directory" --
/// and `blkid` and the mount helpers go through `/sys/class/block`. Partitions
/// resolve one level deeper, because their attributes live under the disk.
pub(super) fn publish_lookup_links(device: &Arc<dyn BlockDevice>, target: &str) -> SysTreeResult<()> {
    let class_block = crate::device::sysfs::block_class_node();
    class_block.add_child(
        crate::device::sysfs::SysfsSymlink::new(device.name(), target) as Arc<dyn SysObj>
    )?;

    let dev_block = crate::device::sysfs::dev_block_node();
    let id = device.id();
    let key = alloc::format!("{}:{}", id.major().get(), id.minor().get());
    dev_block.add_child(
        crate::device::sysfs::SysfsSymlink::new(&key, target) as Arc<dyn SysObj>
    )?;
    Ok(())
}

/// One block device (or partition) directory.
#[derive(Debug)]
struct GenericBlockSysNode {
    fields: BranchNodeFields<dyn SysObj, Self>,
    device: Arc<dyn BlockDevice>,
}

#[inherit_methods(from = "self.fields")]
impl GenericBlockSysNode {
    fn new(device: Arc<dyn BlockDevice>) -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        for name in [
            "dev",
            "size",
            "ro",
            "removable",
            "range",
            "stat",
            "inflight",
        ] {
            builder.add(SysStr::from(name), SysPerms::DEFAULT_RO_ATTR_PERMS);
        }
        if device.is_partition() {
            builder.add(SysStr::from("partition"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        }
        let attrs = builder
            .build()
            .expect("failed to build the block device attribute set");

        let name = device.name().to_string();
        Arc::new_cyclic(|weak_self| Self {
            fields: BranchNodeFields::new(SysStr::from(name), attrs, weak_self.clone()),
            device,
        })
    }

    fn add_child(&self, new_child: Arc<dyn SysObj>) -> SysTreeResult<()>;
}

inherit_sys_branch_node!(GenericBlockSysNode, fields, {
    fn read_attr_at(
        &self,
        name: &str,
        offset: usize,
        writer: &mut VmWriter,
    ) -> SysTreeResult<usize> {
        let device = &self.device;
        let nr_sectors = device.metadata().nr_sectors;
        let mut printer = VmPrinter::new_skip(writer, offset);
        match name {
            "dev" => {
                let id = device.id();
                writeln!(printer, "{}:{}", id.major().get(), id.minor().get())?
            }
            "size" | "range" => writeln!(printer, "{}", nr_sectors)?,
            "ro" => writeln!(printer, "{}", device.is_read_only() as u32)?,
            "removable" => writeln!(printer, "0")?,
            // Reads, read merges, sectors, read ticks, writes, write merges,
            // sectors, write ticks, in flight, ticks, time in queue. Merges and
            // the timing columns have no source here and read as zero; the rest
            // come from the same counters `/proc/diskstats` reports.
            "stat" => {
                let stats = aster_block::iostats::lookup(device.id())
                    .map(|stats| stats.snapshot())
                    .unwrap_or_default();
                writeln!(
                    printer,
                    "{} 0 {} 0 {} 0 {} 0 {} 0 0",
                    stats.reads,
                    stats.read_sectors,
                    stats.writes,
                    stats.write_sectors,
                    stats.in_flight
                )?
            }
            "inflight" => writeln!(printer, "0 0 0 0")?,
            "partition" => {
                let number = partition_number(device.name());
                writeln!(printer, "{}", number)?
            }
            _ => return Err(SysTreeError::AttributeError),
        }
        Ok(printer.bytes_written())
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

/// The partition index in a name like `mmcblk0p2` or `nvme0n1p2`.
///
/// The name is the only place the index is recorded: `PartitionNode` keeps
/// its `PartitionInfo` private and exposes only the size.
fn partition_number(name: &str) -> u32 {
    name.rsplit_once('p')
        .and_then(|(_, number)| number.parse().ok())
        .unwrap_or(0)
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn partition_names_yield_their_index() {
        assert_eq!(partition_number("mmcblk0p2"), 2);
        assert_eq!(partition_number("mmcblk0p11"), 11);
        assert_eq!(partition_number("nvme0n1p3"), 3);
        assert_eq!(partition_number("vda"), 0);
    }
}
