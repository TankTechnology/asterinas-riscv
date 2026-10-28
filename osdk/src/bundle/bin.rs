// SPDX-License-Identifier: MPL-2.0

//! Kernel binary artifacts stored in an OSDK bundle.
//!
//! This module also selects the artifact format used for QEMU-direct boot.

use std::{
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    time::SystemTime,
};

use linux_bzimage_builder::PayloadEncoding;

use super::file::BundleFile;
use crate::{arch::Arch, config::scheme::BootProtocol, util::hard_link_or_copy};

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct AsterBin {
    path: PathBuf,
    arch: Arch,
    typ: AsterBinType,
    version: String,
    modified_time: SystemTime,
    size: u64,
    stripped: bool,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub enum AsterBinType {
    Elf(AsterElfMeta),
    BzImage(AsterBzImageMeta),
    RiscvImage,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum QemuDirectBinKind {
    Elf,
    BzImage,
    RiscvImage,
}

pub(crate) fn expected_qemu_direct_bin_kind(
    arch: Arch,
    boot_protocol: BootProtocol,
) -> QemuDirectBinKind {
    match arch {
        Arch::RiscV64 => QemuDirectBinKind::RiscvImage,
        Arch::X86_64 => match boot_protocol {
            BootProtocol::Linux => QemuDirectBinKind::BzImage,
            BootProtocol::Multiboot | BootProtocol::Multiboot2 => QemuDirectBinKind::Elf,
        },
        Arch::Aarch64 | Arch::LoongArch64 => QemuDirectBinKind::Elf,
    }
}

impl AsterBinType {
    pub(super) fn qemu_direct_kind(&self) -> QemuDirectBinKind {
        match self {
            Self::Elf(_) => QemuDirectBinKind::Elf,
            Self::BzImage(_) => QemuDirectBinKind::BzImage,
            Self::RiscvImage => QemuDirectBinKind::RiscvImage,
        }
    }
}

impl QemuDirectBinKind {
    pub(crate) fn supports_encoding(self, encoding: &PayloadEncoding) -> bool {
        self == Self::BzImage || encoding == &PayloadEncoding::Raw
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct AsterElfMeta {
    pub has_linux_header: bool,
    pub has_pvh_header: bool,
    pub has_multiboot_header: bool,
    pub has_multiboot2_header: bool,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct AsterBzImageMeta {
    pub support_legacy32_boot: bool,
    pub support_efi_boot: bool,
    pub support_efi_handover: bool,
}

impl BundleFile for AsterBin {
    fn path(&self) -> &PathBuf {
        &self.path
    }

    fn modified_time(&self) -> &SystemTime {
        &self.modified_time
    }

    fn size(&self) -> &u64 {
        &self.size
    }
}

impl AsterBin {
    pub fn new(
        path: impl AsRef<Path>,
        arch: Arch,
        typ: AsterBinType,
        version: String,
        stripped: bool,
    ) -> Self {
        let created = Self {
            path: path.as_ref().to_path_buf(),
            arch,
            typ,
            version,
            modified_time: SystemTime::UNIX_EPOCH,
            size: 0,
            stripped,
        };
        Self {
            modified_time: created.get_modified_time(),
            size: created.get_size(),
            ..created
        }
    }

    pub fn arch(&self) -> Arch {
        self.arch
    }

    pub fn version(&self) -> &String {
        &self.version
    }

    pub fn stripped(&self) -> bool {
        self.stripped
    }

    pub(super) fn is_unstripped_elf(&self) -> bool {
        matches!(self.typ, AsterBinType::Elf(_)) && !self.stripped
    }

    pub(super) fn is_valid_debug_elf_for(&self, boot_artifact: &Self) -> bool {
        self.is_unstripped_elf()
            && self.arch == boot_artifact.arch
            && self.version == boot_artifact.version
    }

    pub fn typ(&self) -> &AsterBinType {
        &self.typ
    }

    /// Copy the binary to the `base` directory and convert the path to a relative path.
    pub fn copy_to(self, base: impl AsRef<Path>) -> Self {
        let file_name = self.path.file_name().unwrap();
        let copied_path = base.as_ref().join(file_name);
        hard_link_or_copy(&self.path, &copied_path).unwrap();
        let copied_metadata = copied_path.metadata().unwrap();
        Self {
            path: PathBuf::from(file_name),
            arch: self.arch,
            typ: self.typ,
            version: self.version,
            modified_time: copied_metadata.modified().unwrap(),
            size: copied_metadata.size(),
            stripped: self.stripped,
        }
    }
}

#[cfg(test)]
mod tests {
    use std::fs;

    use linux_bzimage_builder::PayloadEncoding;

    use super::{AsterBin, AsterBinType, AsterElfMeta, QemuDirectBinKind};
    use crate::{arch::Arch, config::scheme::BootProtocol};

    #[test]
    fn qemu_direct_artifact_follows_architecture_policy() {
        let cases = [
            (
                Arch::RiscV64,
                BootProtocol::Linux,
                QemuDirectBinKind::RiscvImage,
            ),
            (
                Arch::RiscV64,
                BootProtocol::Multiboot2,
                QemuDirectBinKind::RiscvImage,
            ),
            (
                Arch::X86_64,
                BootProtocol::Linux,
                QemuDirectBinKind::BzImage,
            ),
            (
                Arch::X86_64,
                BootProtocol::Multiboot2,
                QemuDirectBinKind::Elf,
            ),
            (
                Arch::LoongArch64,
                BootProtocol::Multiboot2,
                QemuDirectBinKind::Elf,
            ),
        ];

        for (arch, protocol, expected) in cases {
            assert_eq!(
                super::expected_qemu_direct_bin_kind(arch, protocol),
                expected,
                "unexpected QEMU-direct artifact for {arch} and {protocol:?}",
            );
        }
    }

    #[test]
    fn riscv_image_only_supports_raw_encoding() {
        assert!(QemuDirectBinKind::RiscvImage.supports_encoding(&PayloadEncoding::Raw));
        assert!(!QemuDirectBinKind::RiscvImage.supports_encoding(&PayloadEncoding::Gzip));
    }

    #[test]
    fn valid_debug_elf_is_unstripped_and_matches_the_boot_artifact() {
        let temp_dir = tempfile::tempdir().unwrap();
        let image_path = temp_dir.path().join("kernel.Image");
        fs::write(&image_path, b"Image").unwrap();
        let image = AsterBin::new(
            image_path,
            Arch::RiscV64,
            AsterBinType::RiscvImage,
            "1.0.0".to_owned(),
            true,
        );
        fn elf_type() -> AsterBinType {
            AsterBinType::Elf(AsterElfMeta {
                has_linux_header: false,
                has_pvh_header: false,
                has_multiboot_header: true,
                has_multiboot2_header: true,
            })
        }

        let cases = [
            ("debug.elf", Arch::RiscV64, "1.0.0", false, true),
            ("stripped.elf", Arch::RiscV64, "1.0.0", true, false),
            ("x86.elf", Arch::X86_64, "1.0.0", false, false),
            ("old.elf", Arch::RiscV64, "2.0.0", false, false),
        ];
        for (name, arch, version, stripped, expected) in cases {
            let path = temp_dir.path().join(name);
            fs::write(&path, b"\x7fELF").unwrap();
            let debug_elf = AsterBin::new(path, arch, elf_type(), version.to_owned(), stripped);

            assert_eq!(debug_elf.is_valid_debug_elf_for(&image), expected);
        }
    }
}
