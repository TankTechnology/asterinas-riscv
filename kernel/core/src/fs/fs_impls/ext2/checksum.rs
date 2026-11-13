// SPDX-License-Identifier: MPL-2.0

//! CRC32C (Castagnoli) checksums used by ext4 metadata checksums.
//!
//! ext4's `metadata_csum` feature protects every metadata structure with a
//! CRC32C, the reflected form of the Castagnoli polynomial 0x1EDC6F41
//! (0x82F63B78 reflected). The kernel computes the checksum incrementally,
//! starting from a seed derived from the filesystem UUID and feeding the
//! structure's fields in order, so the primitive here mirrors that shape:
//! [`crc32c`] continues a previous value instead of always starting from
//! scratch.
//!
//! The result is **not** inverted at the end: ext4 stores the raw running
//! value, and every seed ext4 uses already carries the `~0` the conventional
//! CRC-32C byte string starts with.

const POLYNOMIAL: u32 = 0x82F6_3B78;

/// The checksum table is built once at compile time so the hot path is a
/// table lookup per byte rather than a bit loop.
const TABLE: [u32; 256] = build_table();

const fn build_table() -> [u32; 256] {
    let mut table = [0u32; 256];
    let mut index = 0;
    while index < 256 {
        let mut value = index as u32;
        let mut bit = 0;
        while bit < 8 {
            value = if value & 1 != 0 {
                (value >> 1) ^ POLYNOMIAL
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

/// Continues a CRC32C over `bytes`, starting from `previous`.
///
/// The conventional "CRC-32C of `bytes`" is `crc32c(!0, bytes) ^ !0`; ext4
/// stores `crc32c(seed, bytes)` unchanged, so callers pass the filesystem's
/// seed directly.
pub fn crc32c(previous: u32, bytes: &[u8]) -> u32 {
    let mut value = previous;
    for byte in bytes {
        value = TABLE[((value ^ u32::from(*byte)) & 0xff) as usize] ^ (value >> 8);
    }
    value
}

/// Returns the initial value a plain CRC-32C byte string starts from.
pub const fn initial() -> u32 {
    !0
}

/// Returns ext4's checksum seed for a filesystem UUID.
///
/// Without `metadata_csum_seed` every structure is seeded with the CRC of the
/// UUID; the caller passes an explicit seed when the incompat bit is present.
pub fn fs_seed(uuid: &[u8; 16]) -> u32 {
    crc32c(initial(), uuid)
}

/// Checksums a superblock: the CRC covers the first 1020 bytes, and the
/// `s_checksum` field itself (the last word) is excluded.
///
/// Unlike every other metadata structure, ext4 seeds the superblock checksum
/// with `~0` rather than the filesystem checksum seed.
pub fn superblock(bytes: &[u8; 1024]) -> u32 {
    crc32c(initial(), &bytes[..1020])
}

/// Checksums a block group descriptor.
///
/// ext4 mixes the group number in first, then the descriptor with its
/// `bg_checksum` field zeroed. The descriptor may be 32 or 64 bytes long.
pub fn group_desc(seed: u32, group: u32, desc: &[u8; 64]) -> u32 {
    let mut value = crc32c(seed, &group.to_le_bytes());
    value = crc32c(value, &desc[..0x1E]);
    value = crc32c(value, &[0, 0]);
    crc32c(value, &desc[0x20..])
}

/// Checksums an allocation bitmap (`bg_block_bitmap_csum` /
/// `bg_inode_bitmap_csum`).
pub fn bitmap(seed: u32, bitmap: &[u8]) -> u32 {
    crc32c(seed, bitmap)
}

/// Checksums an inode: the inode number and its generation are mixed in
/// before the raw inode bytes, with `i_checksum_lo`/`i_checksum_hi` zeroed.
///
/// Both words are part of the checksum even though the generation is also
/// stored inside the inode body; e2fsprogs hashes it twice.
pub fn inode(seed: u32, ino: u32, generation: u32, inode: &[u8]) -> u32 {
    let value = crc32c(seed, &ino.to_le_bytes());
    let value = crc32c(value, &generation.to_le_bytes());
    crc32c(value, inode)
}

/// Checksums a directory block: the owning inode and its generation, then the
/// block contents up to (but not including) the 12-byte tail.
pub fn dir_block(seed: u32, ino: u32, generation: u32, block: &[u8]) -> u32 {
    let mut value = crc32c(seed, &ino.to_le_bytes());
    value = crc32c(value, &generation.to_le_bytes());
    crc32c(value, block)
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::{crc32c, initial};

    #[ktest]
    fn matches_the_castagnoli_reference_vector() {
        // The canonical check value of CRC-32C over "123456789" is 0xE3069283.
        assert_eq!(crc32c(initial(), b"123456789") ^ initial(), 0xE306_9283);
    }

    #[ktest]
    fn continues_across_chunks() {
        let whole = crc32c(initial(), b"123456789");
        let split = crc32c(crc32c(initial(), b"1234"), b"56789");
        assert_eq!(whole, split);
    }

    #[ktest]
    fn empty_input_preserves_the_seed() {
        assert_eq!(crc32c(0x1234_5678, b""), 0x1234_5678);
    }

    #[ktest]
    fn does_not_invert_the_final_value() {
        // ext4 stores the running value, so the seed survives an empty input
        // unchanged rather than coming back as its complement.
        assert_eq!(crc32c(0, b""), 0);
        assert_eq!(crc32c(0xffff_ffff, b""), 0xffff_ffff);
        assert_eq!(crc32c(0, b"\0"), 0);
    }
}
