// SPDX-License-Identifier: MPL-2.0

//! Inode metadata operations: mode, ownership, timestamps, and xattr.
//!
//! Ext2 inode metadata includes the file mode, user and group ownership,
//! timestamps, and extended attributes. Attribute mutations must update the
//! relevant change timestamps and mark the inode descriptor dirty so the
//! on-disk inode can be refreshed during writeback.

#![short_vis_path::add(ext2)]

use device_id::DeviceId;

use crate::{
    fs::{
        ext2::{inode::Inode, prelude::*, utils},
        file::InodeMode,
        vfs::{
            inode::Metadata,
            xattr::{PosixAcl, XattrName, XattrNamespace, XattrSetFlags},
        },
    },
    process::{Gid, Uid},
};

impl Inode {
    /// Returns the encoded device ID for special files.
    pub(in ext2) fn device_id(&self) -> u64 {
        debug_assert!(self.type_ == InodeType::CharDevice || self.type_ == InodeType::BlockDevice);

        let inner = self.inner.read();
        match &inner.payload {
            super::InodePayload::Device { device_id } => *device_id,
            _ => 0,
        }
    }

    /// Sets the encoded device ID for special files and persists it.
    pub(in ext2) fn set_device_id(&self, device_id: u64) -> Result<()> {
        if self.type_ != InodeType::CharDevice && self.type_ != InodeType::BlockDevice {
            return_errno!(Errno::EINVAL);
        }
        let mut inner = self.inner.write();
        inner.payload = super::InodePayload::Device { device_id };
        inner.set_ctime(utils::now());
        Ok(())
    }

    /// Returns the inode metadata snapshot for stat-like queries.
    pub(in ext2) fn metadata(&self) -> Metadata {
        let inner = self.inner.read();
        let block_meta = inner.raw_block_ptrs();

        let container_dev_id = match self.fs.upgrade() {
            Some(fs) => fs.block_device().id(),
            None => DeviceId::null(),
        };
        let self_dev_id =
            if self.type_ == InodeType::CharDevice || self.type_ == InodeType::BlockDevice {
                DeviceId::from_encoded_u64(block_meta.read_device_id())
            } else {
                None
            };
        Metadata {
            ino: self.ino as u64,
            size: inner.file_size(),
            optimal_block_size: BLOCK_SIZE,
            nr_sectors_allocated: block_meta.sector_count as usize,
            last_access_at: inner.atime(),
            last_modify_at: inner.mtime(),
            last_meta_change_at: inner.ctime(),
            type_: self.type_,
            mode: inner.mode(),
            nr_hard_links: inner.link_count() as usize,
            uid: Uid::new(inner.uid()),
            gid: Gid::new(inner.gid()),
            container_dev_id,
            self_dev_id,
            birth_at: inner.birth(),
        }
    }

    /// Returns the inode type.
    pub(in ext2) fn inode_type(&self) -> InodeType {
        self.type_
    }

    /// Returns the file permission mode.
    pub(in ext2) fn mode(&self) -> InodeMode {
        let inner = self.inner.read();
        inner.mode()
    }

    /// Sets the file permission mode.
    ///
    /// An access ACL is kept in sync as Linux's `posix_acl_chmod` does: the
    /// owner entry, the group class (mask when present, group entry
    /// otherwise) and the other entry follow the new mode bits.
    pub(in ext2) fn set_mode(&self, mode: InodeMode) -> Result<()> {
        {
            let mut inner = self.inner.write();
            inner.set_mode(mode);
            inner.set_ctime(utils::now());
        }
        self.sync_acl_with_mode(mode)
    }

    fn sync_acl_with_mode(&self, mode: InodeMode) -> Result<()> {
        let Some(xattr) = self.xattr.as_ref() else {
            return Ok(());
        };
        let Some(name) = XattrName::try_from_full_name("system.posix_acl_access") else {
            return Ok(());
        };
        let Some(bytes) = xattr.get_xattr_bytes(name)? else {
            return Ok(());
        };
        let acl = PosixAcl::parse(&bytes)?;
        let Some(updated) = acl.with_mode(mode.bits()) else {
            return Ok(());
        };
        let mut reader = VmReader::from(updated.as_slice()).to_fallible();
        xattr.set_xattr(name, &mut reader, XattrSetFlags::CREATE_OR_REPLACE)
    }

    /// Returns the owner user ID.
    pub(in ext2) fn uid(&self) -> u32 {
        self.inner.read().uid()
    }

    /// Sets the owner user ID.
    pub(in ext2) fn set_uid(&self, uid: u32) -> Result<()> {
        let mut inner = self.inner.write();
        inner.set_uid(uid);
        inner.set_ctime(utils::now());
        Ok(())
    }

    /// Returns the owner group ID.
    pub(in ext2) fn gid(&self) -> u32 {
        self.inner.read().gid()
    }

    /// Sets `i_file_acl` and stages the inode descriptor.
    ///
    /// Callers that stage an xattr block while holding the filesystem's
    /// commit-serialization lock use this so the block and the inode that
    /// references it become durable in the same commit.
    pub(in ext2) fn set_file_acl_staged(&self, bid: u32) -> Result<()> {
        let fs = self.fs()?;
        let mut inner = self.inner.write();
        inner.set_file_acl(bid);
        inner.set_ctime(utils::now());
        inner.write_back_inode_desc(&fs, self.ino)
    }

    /// Sets the owner group ID.
    pub(in ext2) fn set_gid(&self, gid: u32) -> Result<()> {
        let mut inner = self.inner.write();
        inner.set_gid(gid);
        inner.set_ctime(utils::now());
        Ok(())
    }

    /// Returns the last access time.
    pub(in ext2) fn atime(&self) -> Duration {
        self.inner.read().atime()
    }

    /// Sets the last access time.
    pub(in ext2) fn set_atime(&self, time: Duration) {
        self.inner.write().set_atime(time);
    }

    /// Returns the last data modification time.
    pub(in ext2) fn mtime(&self) -> Duration {
        self.inner.read().mtime()
    }

    /// Sets the last data modification time.
    pub(in ext2) fn set_mtime(&self, time: Duration) {
        self.inner.write().set_mtime(time);
    }

    /// Returns the last metadata change time.
    pub(in ext2) fn ctime(&self) -> Duration {
        self.inner.read().ctime()
    }

    /// Sets the last metadata change time.
    pub(in ext2) fn set_ctime(&self, time: Duration) {
        self.inner.write().set_ctime(time);
    }

    /// Reads one extended-attribute value and writes it to `value_writer`.
    pub(in ext2) fn get_xattr(
        &self,
        name: XattrName,
        value_writer: &mut VmWriter,
    ) -> Result<usize> {
        let xattr = self.xattr.as_ref().ok_or_else(|| {
            Error::with_message(Errno::ENODATA, "xattr not supported on this inode type")
        })?;
        xattr.get_xattr(name, value_writer)
    }

    /// Lists extended attribute names in one namespace and writes them to `list_writer`.
    pub(in ext2) fn list_xattr(
        &self,
        namespace: XattrNamespace,
        list_writer: &mut VmWriter,
    ) -> Result<usize> {
        let Some(xattr) = self.xattr.as_ref() else {
            return Ok(0);
        };
        xattr.list_xattr(namespace, list_writer)
    }

    /// Creates or replaces one extended attribute.
    pub(in ext2) fn set_xattr(
        &self,
        name: XattrName,
        value_reader: &mut VmReader,
        flags: XattrSetFlags,
    ) -> Result<()> {
        let xattr = self.xattr.as_ref().ok_or_else(|| {
            Error::with_message(Errno::EPERM, "xattr not supported on this inode type")
        })?;
        xattr.set_xattr(name, value_reader, flags)?;
        let new_bid = xattr.bid();

        let mut inner = self.inner.write();
        inner.set_file_acl(new_bid);
        inner.set_ctime(utils::now());
        Ok(())
    }

    /// Creates or replaces one extended attribute from an in-memory value.
    ///
    /// This is the kernel-internal counterpart of [`Self::set_xattr`], used
    /// when inheriting a directory's default ACL at creation time.
    pub(in ext2) fn set_xattr_value(&self, name: XattrName, value: &[u8]) -> Result<()> {
        let xattr = self.xattr.as_ref().ok_or_else(|| {
            Error::with_message(Errno::EPERM, "xattr not supported on this inode type")
        })?;
        xattr.set_xattr_bytes(name, value.to_vec(), XattrSetFlags::empty())?;
        let new_bid = xattr.bid();

        let mut inner = self.inner.write();
        inner.set_file_acl(new_bid);
        inner.set_ctime(utils::now());
        Ok(())
    }

    /// Removes one extended attribute.
    pub(in ext2) fn remove_xattr(&self, name: XattrName) -> Result<()> {
        let xattr = self.xattr.as_ref().ok_or_else(|| {
            Error::with_message(Errno::EPERM, "xattr not supported on this inode type")
        })?;
        xattr.remove_xattr(name)?;
        let new_bid = xattr.bid();

        let mut inner = self.inner.write();
        inner.set_file_acl(new_bid);
        inner.set_ctime(utils::now());
        Ok(())
    }
}
