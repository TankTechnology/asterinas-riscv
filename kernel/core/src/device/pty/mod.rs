// SPDX-License-Identifier: MPL-2.0

use crate::{
    fs::{
        devpts::{DevPts, Ptmx},
        file::{InodeType, mkmod},
        vfs::{
            path::{FsPath, PathResolver, PerMountFlags},
            registry::FsAndRoot,
        },
    },
    prelude::*,
};

mod driver;
mod file;
mod ioctl_defs;
mod master;
mod packet;

pub(crate) use driver::PtySlave;
pub(crate) use master::PtyMaster;

/// Mounts devpts and publishes the conventional `/dev/ptmx` symlink.
///
/// Linux devtmpfs populates these entries itself. Asterinas creates the
/// directory and symlink explicitly so that `open("/dev/ptmx")` and
/// `pty.fork()` work on a rootfs boot.
pub(crate) fn init_in_first_process(path_resolver: &PathResolver, ctx: &Context) -> Result<()> {
    let dev = path_resolver.lookup(&FsPath::try_from("/dev")?)?;
    let pts = match dev.lookup_child("pts") {
        Ok(pts) => pts,
        Err(_) => dev.new_child("pts", InodeType::Dir, mkmod!(a+rx, u+w))?,
    };
    pts.mount(
        FsAndRoot::new(DevPts::new()),
        PerMountFlags::default(),
        Some("devpts".to_string()),
        ctx,
    )?;

    if dev.lookup_child("ptmx").is_err() {
        dev.new_symlink_child("ptmx", "pts/ptmx", mkmod!(a+rwx))?;
    }

    Ok(())
}

pub(crate) fn new_pty_pair(index: u32, ptmx: Arc<Ptmx>) -> Result<(Box<PtyMaster>, Arc<PtySlave>)> {
    debug!("pty index = {}", index);
    let master = PtyMaster::new(ptmx, index);
    let slave = master.slave().clone();
    Ok((master, slave))
}
