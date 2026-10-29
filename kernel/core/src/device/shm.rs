// SPDX-License-Identifier: MPL-2.0

use crate::{
    fs::{
        file::{InodeMode, InodeType, chmod},
        ramfs::RamFs,
        vfs::{
            path::{FsPath, PathResolver, PerMountFlags},
            registry::FsAndRoot,
        },
    },
    prelude::*,
};

/// Creates and mounts the POSIX shared-memory directory at `/dev/shm`.
///
/// Firefox's content processes, and other applications that use
/// `shm_open(3)`, expect this directory to exist and be writable. Linux
/// distributions provide it through devtmpfs or an early init script.
pub(crate) fn init_in_first_process(path_resolver: &PathResolver, ctx: &Context) -> Result<()> {
    let dev_path = path_resolver.lookup(&FsPath::try_from("/dev")?)?;
    let shm_path = match dev_path.lookup_child("shm") {
        Ok(shm_path) => shm_path,
        Err(_) => dev_path.new_child("shm", InodeType::Dir, chmod!(InodeMode::S_ISVTX, a+rwx))?,
    };
    shm_path.mount(
        FsAndRoot::new(RamFs::new()),
        PerMountFlags::default(),
        Some("tmpfs".to_string()),
        ctx,
    )?;

    Ok(())
}
