// SPDX-License-Identifier: MPL-2.0

#![expect(dead_code)]
#![expect(unused_variables)]

use align_ext::AlignExt;

use super::{
    constants::UNICODE_SIZE,
    dentry::{ExfatDentry, ExfatDentryIterator, ExfatUpcaseDentry, UTF16Char},
    fat::ExfatChain,
    fs::ExfatFs,
    utils::calc_checksum_32,
};
use crate::{fs::exfat::fat::FatChainFlags, prelude::*, vm::page_cache::PageCache};

const UPCASE_MANDATORY_SIZE: usize = 128;
const UPCASE_UNICODE_SIZE: usize = 1 << 16;

#[derive(Debug)]
pub(super) struct ExfatUpcaseTable {
    upcase_table: Vec<u16>,
    fs: Weak<ExfatFs>,
}

impl ExfatUpcaseTable {
    pub(super) fn empty() -> Self {
        Self {
            upcase_table: Vec::new(),
            fs: Weak::default(),
        }
    }

    pub(super) fn load(
        fs_weak: Weak<ExfatFs>,
        root_page_cache: &PageCache,
        root_chain: ExfatChain,
    ) -> Result<Self> {
        let dentry_iterator = ExfatDentryIterator::new(root_page_cache, 0, None)?;

        for dentry_result in dentry_iterator {
            let dentry = dentry_result?;
            if let ExfatDentry::Upcase(upcase_dentry) = dentry {
                return Self::load_table_from_dentry(fs_weak, &upcase_dentry);
            }
        }

        return_errno_with_message!(Errno::EINVAL, "Upcase table not found")
    }

    fn load_table_from_dentry(fs_weak: Weak<ExfatFs>, dentry: &ExfatUpcaseDentry) -> Result<Self> {
        let size = usize::try_from(dentry.size)
            .map_err(|_| Error::with_message(Errno::EINVAL, "invalid upcase table size"))?;
        if size < UPCASE_MANDATORY_SIZE * UNICODE_SIZE
            || size > UPCASE_UNICODE_SIZE * UNICODE_SIZE * 2
            || !size.is_multiple_of(UNICODE_SIZE)
        {
            return_errno_with_message!(Errno::EINVAL, "invalid upcase table size")
        }

        let fs = fs_weak.upgrade().unwrap();
        let num_clusters = size.align_up(fs.cluster_size()) / fs.cluster_size();
        let chain = ExfatChain::new(
            fs_weak.clone(),
            dentry.start_cluster,
            Some(num_clusters as u32),
            FatChainFlags::ALLOC_POSSIBLE,
        )?;

        let mut buf = vec![0; size];
        fs.read_meta_at(chain.physical_cluster_start_offset(), &mut buf)?;

        if dentry.checksum != calc_checksum_32(&buf) {
            return_errno_with_message!(Errno::EINVAL, "invalid checksum")
        }

        let res = ExfatUpcaseTable {
            upcase_table: decode_upcase_table(&buf)?,
            fs: fs_weak,
        };

        Ok(res)
    }

    pub(super) fn str_to_upcase(&self, value: &str) -> Result<Vec<UTF16Char>> {
        value
            .encode_utf16()
            .map(|character| self.char_to_upcase(character))
            .collect()
    }

    pub(super) fn slice_to_upcase(&self, buf: &mut [UTF16Char]) -> Result<()> {
        for value in buf {
            *value = self.char_to_upcase(*value)?;
        }
        Ok(())
    }

    pub(super) fn char_to_upcase(&self, value: UTF16Char) -> Result<UTF16Char> {
        self.upcase_table
            .get(value as usize)
            .copied()
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "upcase table not loaded"))
    }
}

fn decode_upcase_table(data: &[u8]) -> Result<Vec<u16>> {
    if !data.len().is_multiple_of(UNICODE_SIZE) {
        return_errno_with_message!(Errno::EINVAL, "unaligned upcase table")
    }

    let mut mappings = Vec::new();
    mappings
        .try_reserve_exact(UPCASE_UNICODE_SIZE)
        .map_err(|_| Error::with_message(Errno::ENOMEM, "cannot allocate upcase table"))?;
    let mut words = data.chunks_exact(UNICODE_SIZE);
    while let Some(bytes) = words.next() {
        let value = u16::from_le_bytes([bytes[0], bytes[1]]);
        if value == u16::MAX && mappings.len() == u16::MAX as usize {
            mappings.push(value);
        } else if value == u16::MAX {
            let count_bytes = words.next().ok_or_else(|| {
                Error::with_message(Errno::EINVAL, "truncated upcase identity run")
            })?;
            let count = u16::from_le_bytes([count_bytes[0], count_bytes[1]]) as usize;
            if count == 0 || count > UPCASE_UNICODE_SIZE - mappings.len() {
                return_errno_with_message!(Errno::EINVAL, "invalid upcase identity run")
            }
            let start = mappings.len();
            mappings.extend((start..start + count).map(|character| character as u16));
        } else if mappings.len() < UPCASE_UNICODE_SIZE {
            mappings.push(value);
        } else {
            return_errno_with_message!(Errno::EINVAL, "upcase table has trailing mappings")
        }
    }
    if mappings.len() != UPCASE_UNICODE_SIZE {
        return_errno_with_message!(Errno::EINVAL, "incomplete upcase table")
    }
    for character in 0..UPCASE_MANDATORY_SIZE {
        let expected = if (b'a' as usize..=b'z' as usize).contains(&character) {
            (character - 32) as u16
        } else {
            character as u16
        };
        if mappings[character] != expected {
            return_errno_with_message!(Errno::EINVAL, "invalid mandatory upcase mapping")
        }
    }
    Ok(mappings)
}
