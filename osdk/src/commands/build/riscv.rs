// SPDX-License-Identifier: MPL-2.0

//! RISC-V Linux Image sidecar builder.
//!
//! The kernel ELF starts with a 64-byte Linux RISC-V Image header so that the
//! same artifact can be loaded by QEMU (as an ELF) and by U-Boot `booti` (as a
//! raw Image). This module turns the boot ELF into the raw Image and fills in
//! the header's image-size field, which cannot be known by the assembly source
//! or linker script at assemble/link time.

use std::{
    fs::{self, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    process::Command,
};

const IMAGE_HEADER_SIZE_BYTES: u64 = 64;
const IMAGE_SIZE_START_OFFSET: u64 = 0x10;
const IMAGE_TEXT_OFFSET: u64 = 0x200000;
const IMAGE_ENTRY_JUMP: u32 = 0x0400_006f;
const IMAGE_VERSION: u32 = 2;
const IMAGE_MAGIC: &[u8; 8] = b"RISCV\0\0\0";
const IMAGE_MAGIC2: &[u8; 4] = b"RSC\x05";

/// Build `<elf-stem>.Image` next to the boot ELF and fill in its size field.
///
/// The input ELF must have been produced by the RISC-V linker script and must
/// start with the Linux Image header implemented in `bsp_boot.S`.
pub(super) fn make_riscv_image(elf_path: &Path) -> Result<PathBuf, String> {
    let image_extent = elf_image_extent(elf_path)?;
    let image_path = elf_path.with_extension("Image");

    let status = Command::new("rust-objcopy")
        .arg("-O")
        .arg("binary")
        .arg(elf_path)
        .arg(&image_path)
        .status()
        .map_err(|error| format!("failed to run rust-objcopy: {error}"))?;
    if !status.success() {
        return Err(format!(
            "rust-objcopy failed with {status} while creating {}",
            image_path.display()
        ));
    }

    finalize_image_file(&image_path, image_extent)?;
    Ok(image_path)
}

/// Compute the number of bytes the bootloader must copy for the image.
///
/// `p_memsz` is used instead of the file size because `.bss` is loaded as
/// zeros and must be included by `booti`.
fn elf_image_extent(elf_path: &Path) -> Result<u64, String> {
    let bytes = fs::read(elf_path)
        .map_err(|error| format!("failed to read {}: {error}", elf_path.display()))?;
    let elf = xmas_elf::ElfFile::new(&bytes)
        .map_err(|error| format!("failed to parse {}: {error:?}", elf_path.display()))?;

    let entry = elf.header.pt2.entry_point();
    let mut start = u64::MAX;
    let mut end = 0u64;
    for program in elf.program_iter() {
        let program_type = program.get_type().map_err(|error| {
            format!(
                "invalid program header in {}: {error:?}",
                elf_path.display()
            )
        })?;
        if program_type != xmas_elf::program::Type::Load || program.mem_size() == 0 {
            continue;
        }
        let physical_address = program.physical_addr();
        start = start.min(physical_address);
        end = end.max(
            physical_address
                .checked_add(program.mem_size())
                .ok_or_else(|| "RISC-V load segment address overflow".to_owned())?,
        );
    }

    if start == u64::MAX || end <= start {
        return Err(format!(
            "{} has no loadable RISC-V image",
            elf_path.display()
        ));
    }
    // The Image header doubles as the kernel entry instruction, so the first
    // loaded byte must be the ELF entry point.
    if start != entry {
        return Err(format!(
            "RISC-V image starts at {start:#x}, but the ELF entry point is {entry:#x}"
        ));
    }
    Ok(end - start)
}

fn finalize_image_file(image_path: &Path, image_size: u64) -> Result<(), String> {
    if image_size <= IMAGE_HEADER_SIZE_BYTES {
        return Err(format!(
            "RISC-V Image must contain a payload after its {IMAGE_HEADER_SIZE_BYTES}-byte header"
        ));
    }

    let mut image = OpenOptions::new()
        .read(true)
        .write(true)
        .open(image_path)
        .map_err(|error| format!("failed to open {}: {error}", image_path.display()))?;
    let loaded_size = image
        .metadata()
        .map_err(|error| format!("failed to inspect {}: {error}", image_path.display()))?
        .len();
    if loaded_size > image_size {
        return Err(format!(
            "RISC-V Image size {image_size:#x} is smaller than its loaded extent {loaded_size:#x}"
        ));
    }

    let mut header = [0u8; IMAGE_HEADER_SIZE_BYTES as usize];
    image
        .read_exact(&mut header)
        .map_err(|error| format!("failed to read {}: {error}", image_path.display()))?;
    validate_header(&header)?;

    image
        .seek(SeekFrom::Start(IMAGE_SIZE_START_OFFSET))
        .map_err(|error| format!("failed to seek {}: {error}", image_path.display()))?;
    image
        .write_all(&image_size.to_le_bytes())
        .map_err(|error| format!("failed to write {}: {error}", image_path.display()))?;
    image
        .set_len(image_size)
        .map_err(|error| format!("failed to extend {}: {error}", image_path.display()))?;
    Ok(())
}

fn validate_header(header: &[u8; IMAGE_HEADER_SIZE_BYTES as usize]) -> Result<(), String> {
    let entry_jump = u32::from_le_bytes(header[0..4].try_into().unwrap());
    let text_offset = u64::from_le_bytes(header[8..16].try_into().unwrap());
    let version = u32::from_le_bytes(header[0x20..0x24].try_into().unwrap());
    if entry_jump != IMAGE_ENTRY_JUMP
        || text_offset != IMAGE_TEXT_OFFSET
        || version != IMAGE_VERSION
        || &header[0x30..0x38] != IMAGE_MAGIC
        || &header[0x38..0x3c] != IMAGE_MAGIC2
    {
        return Err("unsupported RISC-V Image header".to_owned());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::fs;

    use super::*;

    fn valid_image(image_size: u64) -> Vec<u8> {
        let mut image = vec![0; image_size as usize];
        image[0..4].copy_from_slice(&IMAGE_ENTRY_JUMP.to_le_bytes());
        image[8..16].copy_from_slice(&IMAGE_TEXT_OFFSET.to_le_bytes());
        image[0x20..0x24].copy_from_slice(&IMAGE_VERSION.to_le_bytes());
        image[0x30..0x38].copy_from_slice(IMAGE_MAGIC);
        image[0x38..0x3c].copy_from_slice(IMAGE_MAGIC2);
        image
    }

    #[test]
    fn image_size_is_written_and_file_is_extended() {
        let temp_dir = tempfile::tempdir().unwrap();
        let image_path = temp_dir.path().join("kernel.Image");
        let declared_size = IMAGE_HEADER_SIZE_BYTES + 32;
        let mut image = valid_image(declared_size);
        image.truncate(IMAGE_HEADER_SIZE_BYTES as usize + 8);
        fs::write(&image_path, image).unwrap();

        finalize_image_file(&image_path, declared_size).unwrap();

        let bytes = fs::read(&image_path).unwrap();
        assert_eq!(bytes.len() as u64, declared_size);
        assert_eq!(
            u64::from_le_bytes(bytes[0x10..0x18].try_into().unwrap()),
            declared_size
        );
    }

    #[test]
    fn undersized_or_malformed_images_are_rejected() {
        let temp_dir = tempfile::tempdir().unwrap();
        let image_path = temp_dir.path().join("kernel.Image");

        let mut image = valid_image(IMAGE_HEADER_SIZE_BYTES + 16);
        image[0] ^= 1;
        fs::write(&image_path, image).unwrap();
        assert!(finalize_image_file(&image_path, IMAGE_HEADER_SIZE_BYTES + 16).is_err());

        let image = valid_image(IMAGE_HEADER_SIZE_BYTES + 16);
        fs::write(&image_path, image).unwrap();
        assert!(finalize_image_file(&image_path, IMAGE_HEADER_SIZE_BYTES + 8).is_err());
    }
}
