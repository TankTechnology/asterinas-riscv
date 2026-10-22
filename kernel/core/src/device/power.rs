// SPDX-License-Identifier: MPL-2.0

//! `/sys/power`: the system power-management entry point.
//!
//! Only the `state` attribute exists, and only suspend-to-idle semantics:
//! writing `freeze`, `mem` or `standby` parks the system for a bounded window
//! and returns. This kernel has no ACPI S3 resume trampoline, so the host's
//! `system_wakeup` cannot be the wake source; the window's timer is.

use core::time::Duration;

use aster_systree::{
    BranchNodeFields, Error as SysTreeError, Result as SysTreeResult, SysAttrSetBuilder,
    SysObj, SysPerms, SysStr, inherit_sys_branch_node,
};
use aster_util::printer::VmPrinter;
use inherit_methods_macro::inherit_methods;

use crate::prelude::*;

/// How long `mem`/`freeze` parks the system.
const SUSPEND_WINDOW: Duration = Duration::from_secs(2);
/// Timer wake-ups used to cover the window.
const SUSPEND_ROUNDS: u32 = 20;

/// Publishes `/sys/power`.
pub(super) fn init() -> Result<()> {
    let root = aster_systree::primary_tree().root();
    root.add_child(PowerNode::new() as Arc<dyn SysObj>)
        .map_err(|_| Error::with_message(Errno::EEXIST, "/sys/power is already published"))?;
    Ok(())
}

/// Parks the system for [`SUSPEND_WINDOW`].
///
/// The caller sleeps, so every other task keeps running and nothing is torn
/// down: this is suspend-to-idle, not ACPI S3.
fn suspend_to_idle() {
    info!(
        "pm: suspend-to-idle: entering for {:?}",
        SUSPEND_WINDOW
    );
    for _ in 0..SUSPEND_ROUNDS {
        let _ = crate::time::wait_until_or_timeout(
            || false,
            |_| {},
            Some(SUSPEND_WINDOW / SUSPEND_ROUNDS),
        );
    }
    info!("pm: suspend-to-idle: resumed");
}

#[derive(Debug)]
struct PowerNode {
    fields: BranchNodeFields<dyn SysObj, Self>,
}

#[inherit_methods(from = "self.fields")]
impl PowerNode {
    fn new() -> Arc<Self> {
        let mut builder = SysAttrSetBuilder::new();
        builder.add(SysStr::from("state"), SysPerms::DEFAULT_RW_ATTR_PERMS);
        let attr_set = builder
            .build()
            .expect("failed to build the power attribute set");

        Arc::new_cyclic(|weak_self| Self {
            fields: BranchNodeFields::new(SysStr::from("power".to_string()), attr_set, weak_self.clone()),
        })
    }
}

inherit_sys_branch_node!(PowerNode, fields, {
    fn read_attr_at(
        &self,
        name: &str,
        offset: usize,
        writer: &mut VmWriter,
    ) -> SysTreeResult<usize> {
        if name != "state" {
            return Err(SysTreeError::AttributeError);
        }

        let mut printer = VmPrinter::new_skip(writer, offset);
        write!(printer, "freeze mem\n")?;
        Ok(printer.bytes_written())
    }

    fn write_attr_at(
        &self,
        name: &str,
        _offset: usize,
        reader: &mut VmReader,
    ) -> SysTreeResult<usize> {
        if name != "state" {
            return Err(SysTreeError::AttributeError);
        }

        let mut buffer = [0u8; 32];
        let mut writer = VmWriter::from(&mut buffer[..]).to_fallible();
        let length = reader
            .read_fallible(&mut writer)
            .map_err(|_| SysTreeError::AttributeError)?;
        let text =
            core::str::from_utf8(&buffer[..length]).map_err(|_| SysTreeError::AttributeError)?;

        match text.trim() {
            "freeze" | "mem" | "standby" => {
                suspend_to_idle();
                Ok(length)
            }
            // "disk" (hibernate) and anything else are not supported.
            _ => Err(SysTreeError::InvalidOperation),
        }
    }

    fn perms(&self) -> SysPerms {
        SysPerms::DEFAULT_RO_PERMS
    }
});
