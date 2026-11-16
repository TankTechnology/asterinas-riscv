// SPDX-License-Identifier: MPL-2.0

pub mod file;
mod fs_impls;
pub(crate) mod initramfs;
pub(crate) mod pipe;
pub(crate) mod rootfs;
pub(crate) mod thread_info;
pub mod utils;
pub mod vfs;

pub use fs_impls::devtmpfs;
pub(crate) use fs_impls::{
    cgroupfs, configfs, devpts, exfat, ext2, procfs, pseudofs, ramfs, sysfs, tmpfs,
};

use crate::{
    fs::{
        file::{AccessMode, InodeType, OpenArgs, file_table::FdFlags, mkmod},
        vfs::{
            path::{FsPath, Path, PathResolver, PerMountFlags},
            registry::FsAndRoot,
        },
    },
    init,
    prelude::*,
};

pub(crate) fn init() {
    vfs::init();
    fs_impls::init();
}

pub(crate) fn init_on_each_cpu() {
    fs_impls::init_on_each_cpu();
}

pub(crate) fn init_in_first_kthread(path_resolver: &PathResolver) {
    fs_impls::init_in_first_kthread();
    initramfs::init_in_first_kthread(path_resolver).unwrap();
}

pub(crate) fn init_in_first_process(ctx: &Context) {
    let fs = ctx.thread_local.borrow_fs();
    let path_resolver = fs.resolver().read();

    // The kernel's device nodes (ttys, block devices) live in devtmpfs, so it
    // has to be mounted in both boot paths. An initramfs init that boots
    // another root (for example, stage1 handing off to a disk root) needs the
    // block devices just as much as a direct rootfs init does; without them
    // its root discovery finds nothing.
    {
        let dev_path = lookup_or_create_dev(&path_resolver).unwrap();
        dev_path
            .mount(
                FsAndRoot::new(devtmpfs::singleton().clone()),
                PerMountFlags::default(),
                Some("devtmpfs".to_string()),
                ctx,
            )
            .unwrap();
        // The mount shadows whatever `/dev` the initramfs carried, including
        // its `/dev/console`; make sure the node exists in the new mount.
        initramfs::ensure_dev_console(&path_resolver).unwrap();
    }

    // Initialize the file table for the first process.
    let tty_path = FsPath::try_from("/dev/console").unwrap();
    let stdin = {
        let open_args = OpenArgs::from_modes(AccessMode::O_RDONLY, mkmod!(u+r));
        path_resolver
            .lookup(&tty_path)
            .unwrap()
            .open(open_args)
            .unwrap()
    };
    let stdout = {
        let open_args = OpenArgs::from_modes(AccessMode::O_WRONLY, mkmod!(u+w));
        path_resolver
            .lookup(&tty_path)
            .unwrap()
            .open(open_args)
            .unwrap()
    };
    let stderr = {
        let open_args = OpenArgs::from_modes(AccessMode::O_WRONLY, mkmod!(u+w));
        path_resolver
            .lookup(&tty_path)
            .unwrap()
            .open(open_args)
            .unwrap()
    };

    let mut file_table_ref = ctx.thread_local.borrow_file_table_mut();
    let mut file_table = file_table_ref.unwrap().write();

    file_table
        .insert(Arc::new(stdin), FdFlags::empty())
        .map_err(|(err, _)| err)
        .unwrap();
    file_table
        .insert(Arc::new(stdout), FdFlags::empty())
        .map_err(|(err, _)| err)
        .unwrap();
    file_table
        .insert(Arc::new(stderr), FdFlags::empty())
        .map_err(|(err, _)| err)
        .unwrap();
}

fn lookup_or_create_dev(path_resolver: &PathResolver) -> Result<Path> {
    match path_resolver.lookup(&FsPath::try_from("/dev")?) {
        Err(error) if error.error() == Errno::ENOENT => {
            path_resolver
                .root()
                .new_child("dev", InodeType::Dir, mkmod!(a+rx, u+w))
        }
        result => result,
    }
}
