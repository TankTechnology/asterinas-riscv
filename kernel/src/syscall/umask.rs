// SPDX-License-Identifier: MPL-2.0

use super::SyscallReturn;
use crate::{fs::thread_info::FileCreationMask, prelude::*};

pub fn sys_umask(mask: u16, ctx: &Context) -> Result<SyscallReturn> {
    debug!("mask = 0o{:o}", mask);
    let old_mask = ctx
        .thread_local
        .borrow_fs()
        .swap_umask(normalize_umask(mask));
    Ok(SyscallReturn::Return(old_mask.get() as _))
}

fn normalize_umask(mask: u16) -> FileCreationMask {
    FileCreationMask::try_from(mask & 0o777).unwrap()
}

#[cfg(ktest)]
mod test {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn umask_ignores_bits_outside_file_permissions() {
        assert_eq!(normalize_umask(!0o666).get(), 0o111);
        assert_eq!(normalize_umask(0o1022).get(), 0o022);
    }
}
