// SPDX-License-Identifier: MPL-2.0

use alloc::sync::Arc;
use core::sync::atomic::{AtomicU64, Ordering};

use aster_systree::{Error, MAX_ATTR_SIZE, Result, SysAttrSetBuilder, SysPerms, SysStr};
use aster_util::printer::VmPrinter;
use ostd::mm::{VmReader, VmWriter};

use crate::util::ReadCString;

/// A sub-controller responsible for memory resource management in the cgroup subsystem.
///
/// Note that even if the controller is inactive, it still provides some interfaces
/// like "memory.pressure" for usage.
pub(crate) struct MemoryController {
    /// `u64::MAX` represents the Linux `max` sentinel.
    max_memory: AtomicU64,
    high_memory: AtomicU64,
}

impl MemoryController {
    pub(super) fn init_attr_set(builder: &mut SysAttrSetBuilder, is_root: bool) {
        // These attributes only exist on the non-root cgroup nodes.
        // However, it seems that the `memory.stat` attribute is also present on the root node in practice.
        // Currently the implementation follows the documentation strictly.
        //
        // Reference: <https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html#memory-interface-files>
        if !is_root {
            builder.add(
                SysStr::from("memory.events"),
                SysPerms::DEFAULT_RO_ATTR_PERMS,
            );
            builder.add(SysStr::from("memory.max"), SysPerms::DEFAULT_RW_ATTR_PERMS);
            builder.add(SysStr::from("memory.high"), SysPerms::DEFAULT_RW_ATTR_PERMS);
            builder.add(SysStr::from("memory.stat"), SysPerms::DEFAULT_RO_ATTR_PERMS);
        }
    }

    fn read_limit(limit: &AtomicU64, printer: &mut VmPrinter) -> Result<()> {
        let value = limit.load(Ordering::Relaxed);
        if value == u64::MAX {
            writeln!(printer, "max")?;
        } else {
            writeln!(printer, "{}", value)?;
        }
        Ok(())
    }

    fn write_limit(limit: &AtomicU64, reader: &mut VmReader) -> Result<usize> {
        let (content, len) = reader
            .read_cstring_until_end(MAX_ATTR_SIZE)
            .map_err(|_| Error::PageFault)?;
        let value = content
            .to_str()
            .map_err(|_| Error::InvalidOperation)?
            .trim();
        let value = if value == "max" {
            u64::MAX
        } else {
            value
                .parse::<u64>()
                .map_err(|_| Error::InvalidOperation)?
        };
        limit.store(value, Ordering::Relaxed);
        Ok(len)
    }
}

impl super::SubControl for MemoryController {
    fn read_attr_at(&self, name: &str, offset: usize, writer: &mut VmWriter) -> Result<usize> {
        let mut printer = VmPrinter::new_skip(writer, offset);
        match name {
            "memory.max" => Self::read_limit(&self.max_memory, &mut printer)?,
            "memory.high" => Self::read_limit(&self.high_memory, &mut printer)?,
            _ => return Err(Error::AttributeError),
        }
        Ok(printer.bytes_written())
    }

    fn write_attr(&self, name: &str, reader: &mut VmReader) -> Result<usize> {
        match name {
            "memory.max" => Self::write_limit(&self.max_memory, reader),
            "memory.high" => Self::write_limit(&self.high_memory, reader),
            _ => Err(Error::AttributeError),
        }
    }
}

impl super::SubControlStatic for MemoryController {
    fn new(_is_root: bool, _is_active: bool) -> Self {
        Self {
            max_memory: AtomicU64::new(u64::MAX),
            high_memory: AtomicU64::new(u64::MAX),
        }
    }

    fn type_() -> super::SubCtrlType {
        super::SubCtrlType::Memory
    }

    fn read_from(controller: &super::Controller) -> Arc<super::SubController<Self>> {
        controller.memory.read().get().clone()
    }
}
