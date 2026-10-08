// SPDX-License-Identifier: MPL-2.0

//! RISC-V kernel Image builder.
//!
//! The header follows the
//! [Linux RISC-V boot Image format](https://docs.kernel.org/arch/riscv/boot-image-header.html).

use std::{fs::OpenOptions, io::Read, path::Path, process::Command};

use crate::{
    arch::Arch,
    bundle::{
        bin::{AsterBin, AsterBinType},
        file::BundleFile,
    },
    util,
};

const IMAGE_HEADER_SIZE_BYTES: usize = 64;
const IMAGE_SIZE_START_OFFSET_BYTES: usize = 0x10;
const IMAGE_SIZE_END_OFFSET_BYTES: usize = 0x18;

const FIXED_HEADER_PREFIX: [u8; IMAGE_SIZE_START_OFFSET_BYTES] = [
    0x6f, 0x00, 0x00, 0x04, // `jal x0, +64`
    0, 0, 0, 0, // Reserved instruction
    0x00, 0x00, 0x20, 0x00, 0, 0, 0, 0, // 2-MiB text offset
];

// Flags, version 2, reserved fields, magic values, and PE/COFF offset.
const FIXED_HEADER_SUFFIX: [u8; IMAGE_HEADER_SIZE_BYTES - IMAGE_SIZE_END_OFFSET_BYTES] = [
    0, 0, 0, 0, 0, 0, 0, 0, // Flags
    2, 0, 0, 0, // Version
    0, 0, 0, 0, // Reserved
    0, 0, 0, 0, 0, 0, 0, 0, // Reserved
    b'R', b'I', b'S', b'C', b'V', 0, 0, 0, // Magic
    b'R', b'S', b'C', 5, // Magic 2
    0, 0, 0, 0, // PE/COFF offset
];

pub(super) fn make_riscv_image(
    output_dir: impl AsRef<Path>,
    elf_bin: &AsterBin,
) -> Result<AsterBin, String> {
    if elf_bin.arch() != Arch::RiscV64 {
        return Err(format!("expected a RISC-V ELF, found {}", elf_bin.arch()));
    }

    let image_path = output_dir.as_ref().join(format!(
        "{}.Image",
        util::get_current_crates().remove(0).name
    ));
    convert_elf_to_image(elf_bin.path(), &image_path)?;

    Ok(AsterBin::new(
        image_path,
        Arch::RiscV64,
        AsterBinType::RiscvImage,
        elf_bin.version().clone(),
        elf_bin.stripped(),
    ))
}

fn convert_elf_to_image(
    elf_path: impl AsRef<Path>,
    image_path: impl AsRef<Path>,
) -> Result<(), String> {
    let output = Command::new("rust-objcopy")
        .args(["-O", "binary"])
        .arg(elf_path.as_ref())
        .arg(image_path.as_ref())
        .output()
        .map_err(|error| format!("failed to run rust-objcopy: {error}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "rust-objcopy failed with {}: {}",
            output.status,
            stderr.trim(),
        ));
    }

    finalize_image_file(image_path)
}

fn declared_image_size(header: &[u8; IMAGE_HEADER_SIZE_BYTES]) -> Result<u64, String> {
    if header[..IMAGE_SIZE_START_OFFSET_BYTES] != FIXED_HEADER_PREFIX
        || header[IMAGE_SIZE_END_OFFSET_BYTES..] != FIXED_HEADER_SUFFIX
    {
        return Err("unsupported RISC-V Image header".to_owned());
    }

    Ok(u64::from_le_bytes(
        header[IMAGE_SIZE_START_OFFSET_BYTES..IMAGE_SIZE_END_OFFSET_BYTES]
            .try_into()
            .unwrap(),
    ))
}

fn finalize_image_file(image_path: impl AsRef<Path>) -> Result<(), String> {
    let image_path = image_path.as_ref();
    let mut image = OpenOptions::new()
        .read(true)
        .write(true)
        .open(image_path)
        .map_err(|error| format!("failed to open {}: {error}", image_path.display()))?;
    let loaded_size_bytes = image
        .metadata()
        .map_err(|error| format!("failed to inspect {}: {error}", image_path.display()))?
        .len();
    if loaded_size_bytes <= IMAGE_HEADER_SIZE_BYTES as u64 {
        return Err(format!(
            "Image must contain a payload after its {IMAGE_HEADER_SIZE_BYTES}-byte header"
        ));
    }

    let mut header = [0; IMAGE_HEADER_SIZE_BYTES];
    image
        .read_exact(&mut header)
        .map_err(|error| format!("failed to read {}: {error}", image_path.display()))?;
    let declared_size_bytes = declared_image_size(&header)?;
    if declared_size_bytes < loaded_size_bytes {
        return Err(format!(
            "RISC-V Image size {declared_size_bytes:#x} is smaller than its loaded extent \
             {loaded_size_bytes:#x}",
        ));
    }
    image
        .set_len(declared_size_bytes)
        .map_err(|error| format!("failed to extend {}: {error}", image_path.display()))
}

#[cfg(test)]
mod tests {
    use std::fs;

    use super::{
        FIXED_HEADER_PREFIX, FIXED_HEADER_SUFFIX, IMAGE_HEADER_SIZE_BYTES,
        IMAGE_SIZE_END_OFFSET_BYTES, IMAGE_SIZE_START_OFFSET_BYTES,
    };

    fn valid_image(image_size_bytes: usize) -> Vec<u8> {
        let mut image = vec![0; image_size_bytes];
        image[..IMAGE_SIZE_START_OFFSET_BYTES].copy_from_slice(&FIXED_HEADER_PREFIX);
        image[IMAGE_SIZE_START_OFFSET_BYTES..IMAGE_SIZE_END_OFFSET_BYTES]
            .copy_from_slice(&(image_size_bytes as u64).to_le_bytes());
        image[IMAGE_SIZE_END_OFFSET_BYTES..IMAGE_HEADER_SIZE_BYTES]
            .copy_from_slice(&FIXED_HEADER_SUFFIX);
        image
    }

    #[test]
    fn image_file_is_validated_before_extension() {
        let temp_dir = tempfile::tempdir().unwrap();
        let image_path = temp_dir.path().join("kernel.Image");
        let declared_size_bytes = IMAGE_HEADER_SIZE_BYTES + 8;
        let mut image = valid_image(declared_size_bytes);
        image.truncate(declared_size_bytes - 4);
        fs::write(&image_path, image).unwrap();

        super::finalize_image_file(&image_path).unwrap();

        assert_eq!(
            fs::read(&image_path).unwrap(),
            valid_image(declared_size_bytes)
        );

        let mut image = valid_image(declared_size_bytes);
        image[0] ^= 1;
        fs::write(&image_path, image).unwrap();
        assert!(super::finalize_image_file(&image_path).is_err());

        let mut bad_size = valid_image(declared_size_bytes);
        bad_size[IMAGE_SIZE_START_OFFSET_BYTES..IMAGE_SIZE_END_OFFSET_BYTES]
            .copy_from_slice(&65_u64.to_le_bytes());
        fs::write(&image_path, bad_size).unwrap();
        assert!(super::finalize_image_file(&image_path).is_err());
    }
}
