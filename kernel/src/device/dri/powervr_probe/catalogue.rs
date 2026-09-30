// SPDX-License-Identifier: MPL-2.0

//! Selected PowerVR firmware page-catalogue register transaction.

use ostd::mm::PAGE_SIZE;

// RockOS bf2ec5d5, rgxstartstop.c::RGXWriteKernelCatBase and the pinned
// rgx_cr_defs_km.h. HOST_SECURITY_VERSION=1 uses the unqualified registers.
const MAPPING_CONTEXT: usize = 0xe140;
const MAPPING_BASE: usize = 0xe148;
const MAPPING_BASE_MASK: u32 = 0x0fff_ffff;
// RGX_CR_MMU_CBASE_MAPPING_INVALID_EN. The selected board read back exactly
// this reset value before any catalogue write, with all address bits clear.
const MAPPING_BASE_INVALID: u32 = 0x1000_0000;
const GPU_DMA_LIMIT: usize = 1 << 40;

pub(super) trait CatalogueIo {
    fn read32(&mut self, offset: usize) -> Result<u32, &'static str>;
    fn write32(&mut self, offset: usize, value: u32) -> Result<(), &'static str>;
}

pub(super) fn selected_catalogue_state(
    io: &mut impl CatalogueIo,
) -> Result<(u32, u32), &'static str> {
    Ok((io.read32(MAPPING_CONTEXT)?, io.read32(MAPPING_BASE)?))
}

fn encode_selected_root(root_daddr: usize) -> Result<u32, &'static str> {
    if root_daddr == 0 || root_daddr >= GPU_DMA_LIMIT || !root_daddr.is_multiple_of(PAGE_SIZE) {
        return Err("gpu_catalogue_invalid_root");
    }
    let encoded = u32::try_from(root_daddr >> 12).map_err(|_| "gpu_catalogue_invalid_root")?;
    if encoded & !MAPPING_BASE_MASK != 0 {
        return Err("gpu_catalogue_invalid_root");
    }
    Ok(encoded)
}

/// Verify context 0's catalogue base without changing any GPU register.
/// Release callers use this after a possibly repeated preflight and before
/// the first META reset-release write.
pub(super) fn validate_selected_catalogue(
    io: &mut impl CatalogueIo,
    root_daddr: usize,
) -> Result<(), &'static str> {
    let encoded = encode_selected_root(root_daddr)?;
    let (context, base) = selected_catalogue_state(io)?;
    if context != 0 || base != encoded {
        return Err("gpu_catalogue_installed_state_drift");
    }
    Ok(())
}

/// Install only context 0's base before firmware startup. `touched` is
/// set immediately before the first register write, so the owner can clear
/// the mapping before freeing any page-table DMA after a partial failure.
pub(super) fn install_selected_catalogue(
    io: &mut impl CatalogueIo,
    root_daddr: usize,
    touched: &mut bool,
) -> Result<(), &'static str> {
    let encoded = encode_selected_root(root_daddr)?;
    let (initial_context, initial_base) = selected_catalogue_state(io)?;
    if initial_context != 0 || initial_base != MAPPING_BASE_INVALID {
        return Err("gpu_catalogue_initial_state_drift");
    }
    *touched = true;
    io.write32(MAPPING_CONTEXT, 0)?;
    let observed_context = io.read32(MAPPING_CONTEXT)?;
    if observed_context != 0 {
        return Err("gpu_catalogue_context_readback_mismatch");
    }
    io.write32(MAPPING_BASE, encoded)?;
    let observed_base = io.read32(MAPPING_BASE)?;
    if observed_base != encoded {
        return Err("gpu_catalogue_base_readback_mismatch");
    }
    Ok(())
}

/// Select context 0 before clearing its base. The caller must poison
/// ownership and retain the page-table DMA if either readback fails.
pub(super) fn clear_selected_catalogue(io: &mut impl CatalogueIo) -> Result<(), &'static str> {
    io.write32(MAPPING_CONTEXT, 0)?;
    if io.read32(MAPPING_CONTEXT)? != 0 {
        return Err("gpu_catalogue_clear_failed");
    }
    io.write32(MAPPING_BASE, MAPPING_BASE_INVALID)?;
    if io.read32(MAPPING_BASE)? != MAPPING_BASE_INVALID {
        return Err("gpu_catalogue_clear_failed");
    }
    Ok(())
}

#[cfg(ktest)]
mod tests {
    use alloc::{vec, vec::Vec};

    use ostd::prelude::ktest;

    use super::{clear_selected_catalogue, install_selected_catalogue, CatalogueIo};

    #[ktest]
    fn selected_catalogue_validation_rejects_drift_without_writes() {
        let mut io = FakeRegisters::after_hardware_reset();
        assert_eq!(
            super::validate_selected_catalogue(&mut io, 0x1_f16a_3000),
            Err("gpu_catalogue_installed_state_drift")
        );
        let mut touched = false;
        install_selected_catalogue(&mut io, 0x1_f16a_3000, &mut touched).unwrap();
        io.writes.clear();
        assert_eq!(
            super::validate_selected_catalogue(&mut io, 0x1_f16a_3000),
            Ok(())
        );
        for (context, base) in [(1, 0x1f16a3), (0, 0x1f16a4), (0, 0x101f16a3)] {
            io.context = context;
            io.base = base;
            assert_eq!(
                super::validate_selected_catalogue(&mut io, 0x1_f16a_3000),
                Err("gpu_catalogue_installed_state_drift")
            );
        }
        assert_eq!(
            super::validate_selected_catalogue(&mut io, 0),
            Err("gpu_catalogue_invalid_root")
        );
        assert!(io.writes.is_empty());
    }

    #[derive(Default)]
    struct FakeRegisters {
        context: u32,
        base: u32,
        writes: Vec<(usize, u32)>,
    }

    impl FakeRegisters {
        fn after_hardware_reset() -> Self {
            Self {
                base: 0x1000_0000,
                ..Self::default()
            }
        }
    }

    impl CatalogueIo for FakeRegisters {
        fn read32(&mut self, offset: usize) -> Result<u32, &'static str> {
            match offset {
                0xe140 => Ok(self.context),
                0xe148 => Ok(self.base),
                _ => Err("unexpected_gpu_register"),
            }
        }

        fn write32(&mut self, offset: usize, value: u32) -> Result<(), &'static str> {
            match offset {
                0xe140 => self.context = value,
                0xe148 => self.base = value,
                _ => return Err("unexpected_gpu_register"),
            }
            self.writes.push((offset, value));
            Ok(())
        }
    }

    #[ktest]
    fn selected_catalogue_base_is_encoded_read_back_and_cleared() {
        let mut io = FakeRegisters::after_hardware_reset();
        let mut touched = false;
        assert_eq!(
            install_selected_catalogue(&mut io, 0x1_f16a_3000, &mut touched),
            Ok(())
        );
        assert!(touched);
        assert_eq!(io.context, 0);
        assert_eq!(io.base, 0x1f16a3);
        assert_eq!(clear_selected_catalogue(&mut io), Ok(()));
        assert_eq!(io.base, 0x1000_0000);
        assert_eq!(
            io.writes,
            vec![
                (0xe140, 0),
                (0xe148, 0x1f16a3),
                (0xe140, 0),
                (0xe148, 0x1000_0000)
            ]
        );
    }

    #[ktest]
    fn selected_catalogue_rejects_bad_root_without_register_mutation() {
        let mut io = FakeRegisters::after_hardware_reset();
        let mut touched = false;
        assert!(install_selected_catalogue(&mut io, 0, &mut touched).is_err());
        assert!(install_selected_catalogue(&mut io, 0x1_f16a_3001, &mut touched).is_err());
        assert!(install_selected_catalogue(&mut io, 1 << 40, &mut touched).is_err());
        assert!(io.writes.is_empty());
        assert!(!touched);

        io.base = 0;
        assert!(install_selected_catalogue(&mut io, 0x1_f16a_3000, &mut touched).is_err());
        assert!(io.writes.is_empty());
        assert!(!touched);

        io.base = 7;
        assert!(install_selected_catalogue(&mut io, 0x1_f16a_3000, &mut touched).is_err());
        assert_eq!(io.base, 7);
        assert!(io.writes.is_empty());
        assert!(!touched);
    }

    #[ktest]
    fn selected_catalogue_cleanup_selects_context_zero_before_clearing() {
        struct BankedRegisters {
            context: usize,
            bases: [u32; 8],
        }

        impl CatalogueIo for BankedRegisters {
            fn read32(&mut self, offset: usize) -> Result<u32, &'static str> {
                match offset {
                    0xe140 => Ok(self.context as u32),
                    0xe148 => Ok(self.bases[self.context]),
                    _ => Err("unexpected_gpu_register"),
                }
            }

            fn write32(&mut self, offset: usize, value: u32) -> Result<(), &'static str> {
                match offset {
                    0xe140 => self.context = value as usize,
                    0xe148 => self.bases[self.context] = value,
                    _ => return Err("unexpected_gpu_register"),
                }
                Ok(())
            }
        }

        let mut io = BankedRegisters {
            context: 7,
            bases: [0x1f16a3, 0, 0, 0, 0, 0, 0, 17],
        };
        assert_eq!(clear_selected_catalogue(&mut io), Ok(()));
        assert_eq!(io.context, 0);
        assert_eq!(io.bases[0], 0x1000_0000);
        assert_eq!(io.bases[7], 17);
    }
}
