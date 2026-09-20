// SPDX-License-Identifier: MPL-2.0

use crate::{
    fs::{
        file::{InodeType, mkmod},
        procfs::{
            ProcDir, StaticEntry,
            sys::kernel::{
                asterinas_reboot_watchdog::AsterinasRebootWatchdogFileOps,
                cap_last_cap::CapLastCapFileOps,
                dmesg_restrict::DmesgRestrictFileOps,
                pid_max::PidMaxFileOps,
                random::RandomDirOps,
                tainted::TaintedFileOps,
                uts::{DomainnameFileOps, HostnameFileOps, OsReleaseFileOps, VersionFileOps},
                yama::YamaDirOps,
            },
            template::{
                ListedEntry, ProcDirOps, ReaddirEntry, listed_entries_from_table,
                lookup_child_from_table, visit_listed_entries,
            },
        },
        vfs::inode::Inode,
    },
    prelude::*,
    security::lsm::is_yama_enabled,
};

mod asterinas_reboot_watchdog;
mod cap_last_cap;
mod dmesg_restrict;
mod pid_max;
mod random;
mod tainted;
mod uts;
mod yama;

/// Represents the inode at `/proc/sys/kernel`.
pub struct KernelDirOps;

impl KernelDirOps {
    pub fn new_inode(parent: Weak<dyn Inode>) -> Arc<dyn Inode> {
        // Reference:
        // <https://elixir.bootlin.com/linux/v6.16.5/source/kernel/sysctl.c#L1765>
        // <https://elixir.bootlin.com/linux/v6.16.5/source/fs/proc/proc_sysctl.c#L978>
        ProcDir::new(Self, parent, mkmod!(a+rx))
    }

    const STATIC_ENTRIES: &'static [StaticEntry] = &[
        (
            "asterinas_reboot_watchdog",
            InodeType::File,
            AsterinasRebootWatchdogFileOps::new_inode,
        ),
        (
            "cap_last_cap",
            InodeType::File,
            CapLastCapFileOps::new_inode,
        ),
        (
            "dmesg_restrict",
            InodeType::File,
            DmesgRestrictFileOps::new_inode,
        ),
        ("domainname", InodeType::File, DomainnameFileOps::new_inode),
        ("hostname", InodeType::File, HostnameFileOps::new_inode),
        ("osrelease", InodeType::File, OsReleaseFileOps::new_inode),
        ("pid_max", InodeType::File, PidMaxFileOps::new_inode),
        ("random", InodeType::Dir, RandomDirOps::new_inode),
        ("tainted", InodeType::File, TaintedFileOps::new_inode),
        ("version", InodeType::File, VersionFileOps::new_inode),
    ];
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::*;

    use super::asterinas_reboot_watchdog::{validate_disarm_value, validate_write_offset};
    use crate::prelude::Errno;

    #[ktest]
    fn watchdog_write_must_start_at_offset_zero() {
        assert!(validate_write_offset(0).is_ok());
        assert_eq!(validate_write_offset(1).unwrap_err().error(), Errno::EINVAL);
    }

    #[ktest]
    fn watchdog_write_accepts_only_disarm_value() {
        assert!(validate_disarm_value(0).is_ok());
        assert_eq!(validate_disarm_value(1).unwrap_err().error(), Errno::EINVAL);
        assert_eq!(
            validate_disarm_value(-1).unwrap_err().error(),
            Errno::EINVAL
        );
    }
}

impl ProcDirOps for KernelDirOps {
    fn lookup_child(&self, this_dir: &ProcDir<Self>, name: &str) -> Result<Arc<dyn Inode>> {
        if let Some(child) = lookup_child_from_table(name, Self::STATIC_ENTRIES, |f| {
            (f)(this_dir.this_weak().clone())
        }) {
            return Ok(child);
        }

        if name == "yama" && is_yama_enabled() {
            return Ok(YamaDirOps::new_inode(this_dir.this_weak().clone()));
        }

        return_errno_with_message!(Errno::ENOENT, "the file does not exist");
    }

    fn visit_entries_from_offset<'a, F>(&'a self, offset: usize, visit_fn: F) -> Result<()>
    where
        F: FnMut(ReaddirEntry<'a>) -> Result<()>,
    {
        let yama_entry = is_yama_enabled().then(|| ListedEntry::new("yama", InodeType::Dir));

        visit_listed_entries(
            offset,
            listed_entries_from_table(Self::STATIC_ENTRIES).chain(yama_entry),
            visit_fn,
        )
    }
}
