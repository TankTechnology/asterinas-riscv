// SPDX-License-Identifier: MPL-2.0

use crate::{
    fs::file::file_table::FileTable,
    prelude::*,
    process::{
        CloneFlags, credentials::capabilities::CapSet, posix_thread::ContextPthreadAdminApi,
    },
};

/// Provides administrative APIs for disassociating execution contexts.
pub trait ContextUnshareAdminApi {
    /// Unshares the file table.
    fn unshare_files(&self);
    /// Unshares filesystem attributes.
    fn unshare_fs(&self);
    /// Unshares System V semaphore.
    fn unshare_sysvsem(&self);
    /// Creates and enters new namespaces as specified by the `flags` argument.
    fn unshare_namespaces(&self, flags: CloneFlags) -> Result<()>;
}

impl ContextUnshareAdminApi for Context<'_> {
    fn unshare_files(&self) {
        let mut pthread_file_table = self.posix_thread.file_table().lock();

        let mut thread_local_file_table_ref = self.thread_local.borrow_file_table_mut();
        let thread_local_file_table = thread_local_file_table_ref.unwrap();

        let new_file_table = FileTable::fork_from(&thread_local_file_table.read());

        *pthread_file_table = Some(new_file_table.clone_ro());
        *thread_local_file_table = new_file_table;
    }

    fn unshare_fs(&self) {
        let mut fs_ref = self.thread_local.borrow_fs_mut();
        let new_fs = fs_ref.as_ref().clone();
        *fs_ref = Arc::new(new_fs);
        self.posix_thread.set_fs(fs_ref.clone());
    }

    fn unshare_sysvsem(&self) {
        // TODO: Support unsharing System V semaphore.
        warn!("unsharing System V semaphore is not supported");
    }

    fn unshare_namespaces(&self, flags: CloneFlags) -> Result<()> {
        // Create the new user namespace first: any other namespaces created
        // by the same `unshare` call are owned by it, and the calling process
        // is granted all capabilities within it (so an unprivileged caller
        // can, e.g., create a UTS namespace in the same call).
        // Reference: <https://elixir.bootlin.com/linux/v6.18/source/kernel/fork.c#L3282>.
        if flags.contains(CloneFlags::CLONE_NEWUSER) {
            let creator_euid = self.posix_thread.credentials().euid();
            let new_user_ns = self.thread_local.borrow_user_ns().new_child(creator_euid);

            let credentials = self.credentials_mut();
            credentials.set_permitted_capset(CapSet::all());
            credentials.set_effective_capset(CapSet::all());

            *self.process.user_ns().lock() = new_user_ns.clone();
            *self.thread_local.borrow_user_ns_mut() = new_user_ns;
        }

        let user_ns_ref = self.thread_local.borrow_user_ns();

        let mut pthread_ns_proxy = self.posix_thread.ns_proxy().lock();

        let mut thread_local_ns_proxy_ref = self.thread_local.borrow_ns_proxy_mut();
        let thread_local_ns_proxy = thread_local_ns_proxy_ref.unwrap();

        let new_ns_proxy = thread_local_ns_proxy.new_clone(
            &user_ns_ref,
            self.process.as_ref(),
            self.posix_thread,
            flags,
        )?;

        if flags.contains(CloneFlags::CLONE_NEWNS) {
            self.thread_local
                .borrow_fs()
                .resolver()
                .write()
                .switch_to_mnt_ns(new_ns_proxy.mnt_ns())?;
        }

        *pthread_ns_proxy = Some(new_ns_proxy.clone());
        *thread_local_ns_proxy = new_ns_proxy;

        Ok(())
    }
}
