// SPDX-License-Identifier: MPL-2.0

//! CRC32C and the ext4 metadata checksum layout.
//!
//! With the `metadata_csum` feature, every piece of metadata (superblock,
//! group descriptors, bitmaps, inodes, extent blocks, directory blocks and
//! xattr blocks) carries a CRC32C (Castagnoli) checksum. The seed and the
//! exact byte ranges below follow Linux and e2fsprogs; they were verified
//! against images produced by `mke2fs`/`tune2fs`/`debugfs`.
//!
//! | Object | Seed | Hashed bytes |
//! |---|---|---|
//! | superblock | `!0` | `[0, s_checksum)` |
//! | group descriptor | `s_csum_seed`, group as LE32 | full descriptor with `bg_checksum` zero |
//! | block bitmap | `s_csum_seed` | `blocks_per_group / 8` bytes |
//! | inode bitmap | `s_csum_seed` | `inodes_per_group / 8` bytes |
//! | inode | `s_csum_seed` + ino + generation | full inode slot with both checksum fields zero |
//! | extent block | `s_csum_seed` + ino + generation | `[0, 12 + eh_max * 12)` |
//! | directory block | `s_csum_seed` + ino + generation | `[0, block_size - 12)` |
//! | xattr block | `s_csum_seed`, block number as LE64 | header and entries with `h_checksum` zero |

/// The reflected CRC32C (Castagnoli) polynomial.
const CRC32C_POLY: u32 = 0x82f6_3b78;

/// The initial CRC value used for checksums that are not seeded with the
/// filesystem seed (only the superblock).
pub(super) const CRC32C_INIT: u32 = !0;

const fn build_table() -> [u32; 256] {
    let mut table = [0u32; 256];
    let mut index = 0;
    while index < 256 {
        let mut value = index as u32;
        let mut bit = 0;
        while bit < 8 {
            value = if value & 1 != 0 {
                (value >> 1) ^ CRC32C_POLY
            } else {
                value >> 1
            };
            bit += 1;
        }
        table[index] = value;
        index += 1;
    }
    table
}

static CRC32C_TABLE: [u32; 256] = build_table();

/// Continues a CRC32C computation over `data`.
pub(super) fn crc32c(crc: u32, data: &[u8]) -> u32 {
    let mut value = crc;
    for &byte in data {
        value = CRC32C_TABLE[((value ^ u32::from(byte)) & 0xff) as usize] ^ (value >> 8);
    }
    value
}

/// Derives the filesystem checksum seed from the UUID.
pub(super) fn seed_from_uuid(uuid: &[u8; 16]) -> u32 {
    crc32c(CRC32C_INIT, uuid)
}

/// Builds the per-inode checksum seed from the filesystem seed, the inode
/// number and its generation.
pub(super) fn inode_seed(seed: u32, ino: u32, generation: u32) -> u32 {
    let crc = crc32c(seed, &ino.to_le_bytes());
    crc32c(crc, &generation.to_le_bytes())
}

#[cfg(ktest)]
mod test {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn crc32c_matches_known_vectors() {
        // Standard CRC-32C check value: "123456789" is 0xE3069283.
        assert_eq!(crc32c(CRC32C_INIT, b"123456789"), 0xe306_9283);
        assert_eq!(crc32c(0x1234_5678, b""), 0x1234_5678);
    }
}
