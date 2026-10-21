// SPDX-License-Identifier: MPL-2.0

use crate::{
    fs::{
        file::{InodeMode, InodeType},
        vfs::path::Dentry,
    },
    prelude::*,
    process::{UserNamespace, credentials::capabilities::CapSet, posix_thread::AsPosixThread},
    security::lsm::hooks as lsm_hooks,
};

pub(crate) const XATTR_NAME_MAX_LEN: usize = 255;
pub(crate) const XATTR_VALUE_MAX_LEN: usize = 65536;
pub(crate) const XATTR_LIST_MAX_LEN: usize = 65536;
pub(crate) const SECURITY_CAPABILITY_XATTR_NAME: &str = "security.capability";

/// Clears file privileges after an operation modifies file contents.
pub(in crate::fs) fn clear_file_priv(dentry: &Dentry) -> Result<()> {
    let inode = dentry.inode().as_ref();
    if inode.type_() != InodeType::File {
        return Ok(());
    }

    let xattr_name = XattrName::try_from_full_name(SECURITY_CAPABILITY_XATTR_NAME).unwrap();
    match inode.remove_xattr(dentry, xattr_name) {
        Ok(()) => Ok(()),
        Err(error) if matches!(error.error(), Errno::ENODATA | Errno::EOPNOTSUPP) => Ok(()),
        Err(error) => Err(error),
    }?;

    clear_set_id_bits(dentry)
}

fn clear_set_id_bits(dentry: &Dentry) -> Result<()> {
    let inode = dentry.inode().as_ref();
    let mut mode = inode.mode()?;
    if !mode.intersects(InodeMode::S_ISUID | InodeMode::S_ISGID) {
        return Ok(());
    }

    let current_thread = current_thread!();
    let posix_thread = current_thread.as_posix_thread().unwrap();

    // Callers with `CAP_FSETID` may preserve both set-ID bits across content changes.
    if lsm_hooks::on_capable(lsm_hooks::CapableContext::new(
        UserNamespace::get_init_singleton().as_ref(),
        posix_thread,
        CapSet::FSETID,
    ))
    .is_ok()
    {
        return Ok(());
    }

    let original_mode = mode;
    mode.remove(InodeMode::S_ISUID);

    if mode.contains(InodeMode::S_ISGID) {
        let inode_gid = inode.group()?;
        let credentials = posix_thread.credentials();
        let retain_sgid =
            !mode.contains(InodeMode::S_IXGRP) && credentials.has_group_for_fs_access(inode_gid);

        if !retain_sgid {
            mode.remove(InodeMode::S_ISGID);
        }
    }

    if mode == original_mode {
        return Ok(());
    }
    inode.set_mode(dentry, mode)
}

/// Represents different namespaces with different capabilities
/// for extended attributes (xattrs).
#[repr(u8)]
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, TryFromInt)]
pub(crate) enum XattrNamespace {
    User = 1,
    Trusted = 2,
    System = 3,
    Security = 4,
    // More namespaces can be added here.
}

/// Represents the name of an xattr. It includes both a valid namespace
/// and a full name string slice, which contains the namespace prefix.
///
/// For example, "user.foo" is a valid xattr name, and its namespace
/// is `XattrNamespace::User`.
#[derive(Debug, Clone, Copy, Hash)]
pub(crate) struct XattrName<'a> {
    namespace: XattrNamespace,
    full_name: &'a str,
}

impl XattrNamespace {
    pub(crate) fn try_from_full_name(full_name: &str) -> Option<XattrNamespace> {
        const USER_PREFIX: &str = "user.";
        const TRUSTED_PREFIX: &str = "trusted.";
        const SYSTEM_PREFIX: &str = "system.";
        const SECURITY_PREFIX: &str = "security.";

        if full_name.starts_with(USER_PREFIX) {
            Some(XattrNamespace::User)
        } else if full_name.starts_with(TRUSTED_PREFIX) {
            Some(XattrNamespace::Trusted)
        } else if full_name.starts_with(SYSTEM_PREFIX) {
            Some(XattrNamespace::System)
        } else if full_name.starts_with(SECURITY_PREFIX) {
            Some(XattrNamespace::Security)
        } else {
            None
        }
    }

    pub(crate) fn is_user(&self) -> bool {
        matches!(self, XattrNamespace::User)
    }
}

impl<'a> XattrName<'a> {
    pub(crate) fn try_from_full_name(full_name: &'a str) -> Option<Self> {
        let namespace = XattrNamespace::try_from_full_name(full_name)?;
        Some(Self {
            namespace,
            full_name,
        })
    }

    pub(crate) fn namespace(&self) -> XattrNamespace {
        self.namespace
    }

    pub(crate) const fn full_name(&self) -> &'a str {
        self.full_name
    }

    pub(crate) const fn full_name_len(&self) -> usize {
        self.full_name.len()
    }
}

bitflags::bitflags! {
    /// Flags for setting an xattr value.
    pub(crate) struct XattrSetFlags: u8 {
        /// Creates a new xattr if it doesn't exist, or replaces the value if it does.
        const CREATE_OR_REPLACE = 0;
        /// Creates a new xattr, fails if it already exists.
        const CREATE_ONLY = 1;
        /// Replaces the value of an existing xattr, fails if it doesn't exist.
        const REPLACE_ONLY = 2;
    }
}

/// A parsed POSIX ACL in the ext4 on-disk representation.
///
/// The value starts with a little-endian version number (2), followed by
/// 8-byte entries: a 16-bit tag, permission bits and a 32-bit id.
#[derive(Debug)]
pub(crate) struct PosixAcl {
    entries: Vec<PosixAclEntry>,
    has_mask: bool,
}

#[derive(Clone, Copy, Debug)]
struct PosixAclEntry {
    tag: PosixAclTag,
    perm: u8,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum PosixAclTag {
    UserObj,
    User(u32),
    GroupObj,
    Group(u32),
    Mask,
    Other,
}

impl PosixAcl {
    const VERSION: u32 = 2;
    const TAG_USER_OBJ: u16 = 0x01;
    const TAG_USER: u16 = 0x02;
    const TAG_GROUP_OBJ: u16 = 0x04;
    const TAG_GROUP: u16 = 0x08;
    const TAG_MASK: u16 = 0x10;
    const TAG_OTHER: u16 = 0x20;
    const PERM_ALL: u16 = 0o7;

    /// Parses an ACL value read from `system.posix_acl_access`.
    pub(crate) fn parse(bytes: &[u8]) -> Result<Self> {
        if bytes.len() < 4 || (bytes.len() - 4) % 8 != 0 {
            return_errno_with_message!(Errno::EINVAL, "malformed POSIX ACL");
        }
        let version = u32::from_le_bytes(bytes[0..4].try_into().unwrap());
        if version != Self::VERSION {
            return_errno_with_message!(Errno::EINVAL, "unsupported POSIX ACL version");
        }

        let mut entries = Vec::new();
        let mut has_mask = false;
        for chunk in bytes[4..].chunks_exact(8) {
            let raw_tag = u16::from_le_bytes([chunk[0], chunk[1]]);
            let raw_perm = u16::from_le_bytes([chunk[2], chunk[3]]);
            let id = u32::from_le_bytes(chunk[4..8].try_into().unwrap());
            if raw_perm & !Self::PERM_ALL != 0 {
                return_errno_with_message!(Errno::EINVAL, "invalid POSIX ACL permission bits");
            }
            let tag = match raw_tag {
                Self::TAG_USER_OBJ => PosixAclTag::UserObj,
                Self::TAG_USER => PosixAclTag::User(id),
                Self::TAG_GROUP_OBJ => PosixAclTag::GroupObj,
                Self::TAG_GROUP => PosixAclTag::Group(id),
                Self::TAG_MASK => {
                    has_mask = true;
                    PosixAclTag::Mask
                }
                Self::TAG_OTHER => PosixAclTag::Other,
                _ => return_errno_with_message!(Errno::EINVAL, "invalid POSIX ACL tag"),
            };
            entries.push(PosixAclEntry {
                tag,
                perm: raw_perm as u8,
            });
        }
        Ok(Self { entries, has_mask })
    }

    /// Returns the permission bits granted to the caller (read = 4,
    /// write = 2, exec = 1), following Linux `posix_acl_permission`.
    ///
    /// The owner entry wins outright; the group class is the union of the
    /// owning-group entry and every matching named group entry, masked when
    /// the ACL has a mask; everything else uses the other entry.
    pub(crate) fn granted_permissions(
        &self,
        fsuid: u32,
        fsgid: u32,
        in_group: impl Fn(u32) -> bool,
        owner: u32,
        file_gid: u32,
    ) -> u8 {
        if fsuid == owner {
            return self.perm_of(PosixAclTag::UserObj).unwrap_or(0);
        }

        // A named user belongs to the group class: the mask applies.
        for entry in &self.entries {
            if let PosixAclTag::User(id) = entry.tag
                && id == fsuid
            {
                return self.apply_mask(entry.perm);
            }
        }

        let mut group_class = None;
        if fsgid == file_gid || in_group(file_gid) {
            group_class = Some(self.perm_of(PosixAclTag::GroupObj).unwrap_or(0));
        }
        for entry in &self.entries {
            if let PosixAclTag::Group(id) = entry.tag
                && in_group(id)
            {
                group_class = Some(group_class.unwrap_or(0) | entry.perm);
            }
        }

        match group_class {
            Some(bits) => self.apply_mask(bits),
            None => self.perm_of(PosixAclTag::Other).unwrap_or(0),
        }
    }

    fn apply_mask(&self, bits: u8) -> u8 {
        if self.has_mask {
            bits & self.perm_of(PosixAclTag::Mask).unwrap_or(0)
        } else {
            bits
        }
    }

    fn perm_of(&self, tag: PosixAclTag) -> Option<u8> {
        self.entries.iter().find(|e| e.tag == tag).map(|e| e.perm)
    }

    /// Returns the ACL value with its owner, group-class and other entries
    /// rewritten from `mode`, following Linux `posix_acl_chmod`.
    ///
    /// An ACL with a mask has the mask set from the group bits and keeps the
    /// group entry; a minimal ACL (only owner, group and other) has its group
    /// entry rewritten.  Anything else is malformed and left untouched.
    pub(crate) fn with_mode(&self, mode: u16) -> Option<Vec<u8>> {
        let owner = ((mode >> 6) & 0o7) as u8;
        let group = ((mode >> 3) & 0o7) as u8;
        let other = (mode & 0o7) as u8;
        let has_named = self
            .entries
            .iter()
            .any(|e| matches!(e.tag, PosixAclTag::User(_) | PosixAclTag::Group(_)));

        let mut entries = self.entries.clone();
        if self.has_mask {
            for entry in &mut entries {
                match entry.tag {
                    PosixAclTag::UserObj => entry.perm = owner,
                    PosixAclTag::Mask => entry.perm = group,
                    PosixAclTag::Other => entry.perm = other,
                    _ => {}
                }
            }
        } else if !has_named {
            for entry in &mut entries {
                match entry.tag {
                    PosixAclTag::UserObj => entry.perm = owner,
                    PosixAclTag::GroupObj => entry.perm = group,
                    PosixAclTag::Other => entry.perm = other,
                    _ => {}
                }
            }
        } else {
            return None;
        }

        Some(Self::encode(&entries))
    }

    /// Serializes entries back into the ext4 ACL value.
    fn encode(entries: &[PosixAclEntry]) -> Vec<u8> {
        let mut out = Vec::with_capacity(4 + entries.len() * 8);
        out.extend_from_slice(&Self::VERSION.to_le_bytes());
        for entry in entries {
            let (tag, id) = match entry.tag {
                PosixAclTag::UserObj => (Self::TAG_USER_OBJ, u32::MAX),
                PosixAclTag::User(id) => (Self::TAG_USER, id),
                PosixAclTag::GroupObj => (Self::TAG_GROUP_OBJ, u32::MAX),
                PosixAclTag::Group(id) => (Self::TAG_GROUP, id),
                PosixAclTag::Mask => (Self::TAG_MASK, u32::MAX),
                PosixAclTag::Other => (Self::TAG_OTHER, u32::MAX),
            };
            out.extend_from_slice(&tag.to_le_bytes());
            out.extend_from_slice(&u16::from(entry.perm).to_le_bytes());
            out.extend_from_slice(&id.to_le_bytes());
        }
        out
    }
}
