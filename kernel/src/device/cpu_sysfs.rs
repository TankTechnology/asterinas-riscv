// SPDX-License-Identifier: MPL-2.0

//! `/sys/devices/system/cpu`: the CPU topology `lscpu`, `htop` and every
//! "what machine is this" script reads.
//!
//! Linux describes CPUs in `/sys/devices/system/cpu`: the first file `lscpu`
//! opens is `possible`, and without it the tool exits before printing a single
//! line. The directory has no counterpart in this kernel's device model -- the
//! CPUs are not devices -- so it is built here from the scheduler's own view.
//!
//! The board's cores are identical, there is no SMT and one package, so every
//! core reports itself as its own thread sibling and the whole CPU set as its
//! core siblings. The binary cpumask files are emitted as well as the `_list`
//! ones because older util-linux reads only those.

use alloc::string::String;

use aster_systree::{
    AttrLessBranchNodeFields, BranchNodeFields, Result as SysTreeResult, SysAttrSetBuilder,
    SysObj, SysPerms, SysStr, inherit_sys_branch_node,
};
use aster_util::printer::VmPrinter;
use inherit_methods_macro::inherit_methods;
use ostd::cpu::{all_cpus, num_cpus};

use crate::prelude::*;

/// The highest CPU id the kernel could ever describe.
///
/// Linux reports `CONFIG_NR_CPUS - 1`; nothing here is compiled against a
/// fixed ceiling, so this mirrors the usual 64-CPU build that RISC-V uses.
const KERNEL_MAX_CPU: usize = 63;

static SYSTEM_NODE: spin::Once<Arc<crate::device::sysfs::AttrLessSysNode>> = spin::Once::new();

pub(super) fn init_in_first_process() -> SysTreeResult<()> {
    let devices = crate::device::sysfs::devices_node();
    let system = SYSTEM_NODE.call_once(|| {
        let node = crate::device::sysfs::AttrLessSysNode::new("system");
        devices
            .add_child(node.clone() as Arc<dyn SysObj>)
            .expect("failed to create /sys/devices/system");
        node
    });
    let cpu = CpuRootSysNode::new();
    system.add_child(cpu.clone() as Arc<dyn SysObj>)?;

    for cpu_id in all_cpus() {
        let node = CpuSysNode::new(u32::from(cpu_id) as usize);
        cpu.add_child(node as Arc<dyn SysObj>)?;
    }
    Ok(())
}

/// `/sys/devices/system/cpu`.
#[derive(Debug)]
struct CpuRootSysNode {
    fields: BranchNodeFields<dyn SysObj, Self>,
}

#[inherit_methods(from = "self.fields")]
impl CpuRootSysNode {
    fn new() -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        for name in ["possible", "present", "online", "offline", "kernel_max"] {
            builder.add(SysStr::from(name), SysPerms::DEFAULT_RO_ATTR_PERMS);
        }
        let attrs = builder
            .build()
            .expect("failed to build the /sys/devices/system/cpu attribute set");
        Arc::new_cyclic(|weak_self| Self {
            fields: BranchNodeFields::new(SysStr::from("cpu"), attrs, weak_self.clone()),
        })
    }

    fn add_child(&self, new_child: Arc<dyn SysObj>) -> SysTreeResult<()>;
}

inherit_sys_branch_node!(CpuRootSysNode, fields, {
    fn read_attr_at(
        &self,
        name: &str,
        offset: usize,
        writer: &mut VmWriter,
    ) -> SysTreeResult<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        match name {
            // Every core this kernel describes is online for the whole boot.
            "possible" | "present" | "online" => writeln!(printer, "{}", cpu_list(0, num_cpus()))?,
            // An empty list, which is what Linux prints when none is offline.
            "offline" => writeln!(printer)?,
            "kernel_max" => writeln!(printer, "{}", KERNEL_MAX_CPU)?,
            _ => return Err(aster_systree::Error::AttributeError),
        }
        Ok(printer.bytes_written())
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

/// `/sys/devices/system/cpu/cpuN`.
#[derive(Debug)]
struct CpuSysNode {
    fields: BranchNodeFields<dyn SysObj, Self>,
    cpu_id: usize,
}

#[inherit_methods(from = "self.fields")]
impl CpuSysNode {
    fn new(cpu_id: usize) -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        builder.add(SysStr::from("online"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        let attrs = builder
            .build()
            .expect("failed to build the per-cpu attribute set");
        let node = Arc::new_cyclic(|weak_self| Self {
            fields: BranchNodeFields::new(
                SysStr::from(alloc::format!("cpu{}", cpu_id)),
                attrs,
                weak_self.clone(),
            ),
            cpu_id,
        });
        node.add_child(TopologySysNode::new(cpu_id) as Arc<dyn SysObj>)
            .expect("failed to register the cpu topology node");
        node
    }

    fn add_child(&self, new_child: Arc<dyn SysObj>) -> SysTreeResult<()>;
}

inherit_sys_branch_node!(CpuSysNode, fields, {
    fn read_attr_at(
        &self,
        name: &str,
        offset: usize,
        writer: &mut VmWriter,
    ) -> SysTreeResult<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        match name {
            "online" => writeln!(printer, "1")?,
            _ => return Err(aster_systree::Error::AttributeError),
        }
        Ok(printer.bytes_written())
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

/// `/sys/devices/system/cpu/cpuN/topology`.
#[derive(Debug)]
struct TopologySysNode {
    fields: BranchNodeFields<dyn SysObj, Self>,
    cpu_id: usize,
}

impl TopologySysNode {
    fn new(cpu_id: usize) -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        for name in [
            "core_id",
            "physical_package_id",
            "thread_siblings",
            "thread_siblings_list",
            "core_siblings",
            "core_siblings_list",
        ] {
            builder.add(SysStr::from(name), SysPerms::DEFAULT_RO_ATTR_PERMS);
        }
        let attrs = builder
            .build()
            .expect("failed to build the topology attribute set");
        Arc::new_cyclic(|weak_self| Self {
            fields: BranchNodeFields::new(SysStr::from("topology"), attrs, weak_self.clone()),
            cpu_id,
        })
    }
}

inherit_sys_branch_node!(TopologySysNode, fields, {
    fn read_attr_at(
        &self,
        name: &str,
        offset: usize,
        writer: &mut VmWriter,
    ) -> SysTreeResult<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        match name {
            // One thread per core, one package: the core is its own thread
            // sibling and the whole machine is its core sibling.
            "core_id" => writeln!(printer, "{}", self.cpu_id)?,
            "physical_package_id" => writeln!(printer, "0")?,
            "thread_siblings_list" => writeln!(printer, "{}", cpu_list(self.cpu_id, 1))?,
            "core_siblings_list" => writeln!(printer, "{}", cpu_list(0, num_cpus()))?,
            "thread_siblings" => writeln!(printer, "{}", cpumask(self.cpu_id))?,
            "core_siblings" => writeln!(printer, "{}", cpumask_range(0, num_cpus()))?,
            _ => return Err(aster_systree::Error::AttributeError),
        }
        Ok(printer.bytes_written())
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});

/// A CPU list in the form Linux uses: `0-3`, `0,2-3`, `1`.
fn cpu_list(first: usize, count: usize) -> String {
    if count == 0 {
        return String::new();
    }
    if count == 1 {
        return alloc::format!("{}", first);
    }
    alloc::format!("{}-{}", first, first + count - 1)
}

/// A cpumask with a single CPU set, as one 32-bit hex word.
fn cpumask(cpu_id: usize) -> String {
    cpumask_range(cpu_id, 1)
}

/// A cpumask covering `[first, first + count)`, word by word from the lowest.
fn cpumask_range(first: usize, count: usize) -> String {
    let end = first.saturating_add(count);
    let words = end.div_ceil(32).max(1);
    let mut text = String::new();
    for word in 0..words {
        let mut value: u32 = 0;
        for bit in 0..32 {
            let cpu = word * 32 + bit;
            if cpu >= first && cpu < end {
                value |= 1 << bit;
            }
        }
        if word > 0 {
            text.push(',');
        }
        text.push_str(&alloc::format!("{:08x}", value));
    }
    text
}
