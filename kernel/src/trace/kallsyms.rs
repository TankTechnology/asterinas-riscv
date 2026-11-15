// SPDX-License-Identifier: MPL-2.0

//! The kernel's own symbol table: address to function name.
//!
//! Tracing wants to print the function a program counter landed in, and a
//! sampling profiler wants to aggregate by name. Both need a table that only
//! exists once the kernel has been linked, so it is not built by the compiler
//! at all: a fixed region is reserved here, and a build-time tool
//! (`tools/riscv/ksym/kallsyms.py`) fills it after linking by writing into the
//! image file. An image that was not run through the tool simply has a zeroed
//! region, and every lookup answers `None`.
//!
//! Layout, all little endian, relative to the start of [`BLOB`]'s `data`:
//!
//! ```text
//! 0   u32  version      1
//! 4   u32  num_syms
//! 8   u32  table_off    offset of the entry table
//! 12  u32  names_off    offset of the name blob
//! 16  u32  names_len
//! 20  u32  reserved
//! 24  u64  addr_base    entry offsets are relative to this
//! 32       entry table: `num_syms` * (u32 off, u32 name_off), sorted by off
//! ...      names: NUL-terminated, deduplicated
//! ```

use core::sync::atomic::{AtomicU32, Ordering};

use aster_util::printer::VmPrinter;

use crate::prelude::*;

/// The magic the build-time tool looks for. Changing it invalidates images
/// patched by an older tool, which is why it carries a version.
const MAGIC: [u8; 16] = *b"ASTERKALLSYMSv1\0";

/// Bytes reserved for the table and the names. A measured kernel needs about
/// 1.1 MiB (79k symbols, 8 bytes each, plus deduplicated names truncated to
/// 72 bytes); the rest is headroom for growth. The tool refuses to write a
/// table that does not fit rather than truncate it.
const BLOB_SIZE: usize = 2 * 1024 * 1024;

const VERSION: u32 = 1;
const HEADER_LEN: usize = 32;

#[repr(C, align(64))]
struct Blob {
    magic: [u8; 16],
    data: [u8; BLOB_SIZE],
}

// `#[used]` keeps it through codegen; `blob()` below keeps the linker from
// dropping it as unreferenced.
#[used]
static BLOB: Blob = Blob {
    magic: MAGIC,
    data: [0; BLOB_SIZE],
};

/// Returns the reserved region, so the linker sees it as used and the parser
/// below has one place to read from.
fn blob() -> &'static [u8] {
    &BLOB.data
}

// Volatile, and every read of the blob must go through it: the bytes are
// written into the linked image by a build-time tool, so the compiler's own
// view of the initializer (zeros) is wrong and it will happily fold a
// non-volatile read of a constant index into 0. Volatile reads are the only
// way to say that in safe Rust, hence the expectation on the two functions.
#[expect(unsafe_code)]
fn read_u32(at: usize) -> u32 {
    unsafe { core::ptr::read_volatile(blob().as_ptr().add(at) as *const u32) }
}

#[expect(unsafe_code)]
fn read_u64(at: usize) -> u64 {
    // Safety and rationale as in `read_u32`; `at` is 8-aligned by the layout
    // the tool writes.
    unsafe { core::ptr::read_volatile(blob().as_ptr().add(at) as *const u64) }
}

/// Number of symbols in the table, zero when the image was not patched.
pub fn symbol_count() -> usize {
    if read_u32(0) != VERSION {
        return 0;
    }
    read_u32(4) as usize
}

/// Answers whether an image carries a usable table.
pub fn is_available() -> bool {
    symbol_count() > 0
}

struct Layout {
    num_syms: usize,
    table_off: usize,
    names_off: usize,
    addr_base: u64,
}

fn layout() -> Option<Layout> {
    let num_syms = symbol_count();
    if num_syms == 0 {
        return None;
    }
    let table_off = read_u32(8) as usize;
    let names_off = read_u32(12) as usize;
    let names_len = read_u32(16) as usize;
    // The tool can only have written inside the reserved region; a header
    // pointing outside it is corruption, not a table.
    let end = table_off
        .checked_add(num_syms.checked_mul(8)?)?
        .max(names_off.checked_add(names_len)?);
    if end > BLOB_SIZE || table_off < HEADER_LEN || names_off < HEADER_LEN {
        return None;
    }
    Some(Layout {
        num_syms,
        table_off,
        names_off,
        addr_base: read_u64(24),
    })
}

fn entry(layout: &Layout, index: usize) -> (u64, usize) {
    let at = layout.table_off + index * 8;
    let off = read_u32(at) as u64;
    let name_off = read_u32(at + 4) as usize;
    (layout.addr_base + off, layout.names_off + name_off)
}

fn name_at(offset: usize) -> &'static str {
    let bytes = &blob()[offset..];
    let len = bytes.iter().position(|&b| b == 0).unwrap_or(bytes.len());
    core::str::from_utf8(&bytes[..len]).unwrap_or("<invalid>")
}

/// The physical address range the image is linked into, `0x8000_0000..`.
const IMAGE_PHYS_BASE: u64 = 0x8000_0000;
/// The high half the kernel runs in above it.
const KERNEL_VIRT_BASE: u64 = 0xffff_ffff_0000_0000;

/// Normalizes an address that may still be a physical one.
///
/// The kernel is linked into the high half but loaded physically at
/// `0x8020_0000`; early boot runs on the identity mapping, everything after
/// the jump to the high half reports virtual addresses. Both forms name the
/// same bytes, so a lookup may arrive with either. Anything outside the image
/// (user space, MMIO) is passed through unchanged and simply finds no symbol.
fn normalize(addr: u64) -> u64 {
    if (IMAGE_PHYS_BASE..IMAGE_PHYS_BASE + 0x1000_0000).contains(&addr) {
        addr | KERNEL_VIRT_BASE
    } else {
        addr
    }
}

/// Returns the name of the function containing `addr`, if the table has one.
///
/// The table is sorted by address; the answer is the last entry at or below
/// the address, which is how a symbol table is normally read for a program
/// counter.
pub fn lookup(addr: u64) -> Option<&'static str> {
    let layout = layout()?;
    let target = normalize(addr);

    let mut start = 0;
    let mut end = layout.num_syms;
    while start < end {
        let mid = start + (end - start) / 2;
        if entry(&layout, mid).0 <= target {
            start = mid + 1;
        } else {
            end = mid;
        }
    }
    if start == 0 {
        return None;
    }
    let (_, name_off) = entry(&layout, start - 1);
    Some(name_at(name_off))
}

/// Like [`lookup`], but also reports how far past the symbol the address is.
pub fn lookup_with_offset(addr: u64) -> Option<(&'static str, u64)> {
    let layout = layout()?;
    let target = normalize(addr);

    let mut start = 0;
    let mut end = layout.num_syms;
    while start < end {
        let mid = start + (end - start) / 2;
        if entry(&layout, mid).0 <= target {
            start = mid + 1;
        } else {
            end = mid;
        }
    }
    if start == 0 {
        return None;
    }
    let (sym_addr, name_off) = entry(&layout, start - 1);
    Some((name_at(name_off), target - sym_addr))
}

/// Calls `visit` for every symbol, in address order, as `(addr, name)`.
pub fn for_each_symbol<F: FnMut(u64, &'static str)>(mut visit: F) {
    let Some(layout) = layout() else {
        return;
    };
    for index in 0..layout.num_syms {
        let (addr, name_off) = entry(&layout, index);
        visit(addr, name_at(name_off));
    }
}

/// Renders the whole table the way `/proc/kallsyms` does.
///
/// The file is read in 4 KiB chunks and every call starts from `offset`; a
/// full table is several megabytes, so the entry index of the last read is
/// cached: sequential readers (`cat`, `grep`) then cost the new lines only.
pub fn render_from(offset: usize, writer: &mut VmWriter) -> Result<usize> {
    static CURSOR: AtomicU32 = AtomicU32::new(0);

    let Some(layout) = layout() else {
        return Ok(0);
    };

    // Byte offset of each entry's line. Recomputed from the cached index when
    // the reader is sequential, from the start otherwise.
    let mut index = (CURSOR.load(Ordering::Relaxed) as usize).min(layout.num_syms);
    let mut line_start = 0usize;
    for i in 0..index {
        line_start += line_len(&layout, i);
    }
    if line_start > offset {
        index = 0;
        line_start = 0;
    }
    while line_start < offset && index < layout.num_syms {
        line_start += line_len(&layout, index);
        index += 1;
    }

    let mut skip = offset - line_start;
    let mut room = writer.avail();
    let mut printer = VmPrinter::new_skip(writer, skip);
    while index < layout.num_syms {
        let len = line_len(&layout, index);
        let emitted = len.saturating_sub(skip);
        if emitted > room {
            break;
        }
        let (addr, name_off) = entry(&layout, index);
        writeln!(printer, "{:016x} t {}", addr, name_at(name_off))?;
        room -= emitted;
        skip = skip.saturating_sub(len);
        index += 1;
    }
    CURSOR.store(index as u32, Ordering::Relaxed);

    Ok(printer.bytes_written())
}

/// Length of one line as `/proc/kallsyms` renders it, without rendering it.
fn line_len(layout: &Layout, index: usize) -> usize {
    let (_, name_off) = entry(layout, index);
    let bytes = &blob()[name_off..];
    let name_len = bytes.iter().position(|&b| b == 0).unwrap_or(bytes.len());
    // 16 hex digits, " t ", the name, and a newline.
    20 + name_len
}

#[cfg(ktest)]
mod test {
    use ostd::prelude::ktest;

    use super::*;

    // The test image is built by `cargo osdk test`, which does not go through
    // the patching tool, so the table is empty here. What these tests pin down
    // is that an unpatched image behaves (empty, no panic) instead of reading
    // garbage: the tool and the kernel must agree on "not patched".

    #[ktest]
    fn unpatched_image_has_no_symbols() {
        assert!(!is_available());
        assert_eq!(symbol_count(), 0);
        assert!(lookup(0xffff_ffff_8020_0000).is_none());
        assert!(lookup(0x8020_0000).is_none());
    }

    #[ktest]
    fn address_normalization() {
        assert_eq!(normalize(0x8024_5000), 0xffff_ffff_8024_5000);
        assert_eq!(normalize(0xffff_ffff_8024_5000), 0xffff_ffff_8024_5000);
        // A user address is not an image address and is left alone.
        assert_eq!(normalize(0x10_0000), 0x10_0000);
    }
}
