// SPDX-License-Identifier: MPL-2.0

//! Selected Megrez META firmware-interface address contract.

use super::dma::{FW_HEAP_BASE, FW_RAW_HEAP_SIZE};

// RockOS bf2ec5d5 rgxfwutils.c:RGXSetFirmwareAddress and rgx_meta.h.
const META_DATA_BASE: u32 = 0x1000_0000;
const META_UNCACHED: u32 = 0x8000_0000;
const SLC_UNCACHED: u32 = 0x6000_0000;

/// Convert a GPU VA in the selected firmware raw heap to the META pointer ABI.
pub(super) fn meta_fwif_address(
    gpu_virt: usize,
    firmware_cached: bool,
    gpu_cached: bool,
) -> Result<u32, &'static str> {
    if !gpu_virt.is_multiple_of(4) {
        return Err("gpu_fwif_unaligned_address");
    }
    let offset = gpu_virt
        .checked_sub(FW_HEAP_BASE)
        .ok_or("gpu_fwif_outside_raw_heap")?;
    if offset >= FW_RAW_HEAP_SIZE {
        return Err("gpu_fwif_outside_raw_heap");
    }
    let offset = u32::try_from(offset).map_err(|_| "gpu_fwif_outside_raw_heap")?;
    Ok(META_DATA_BASE
        | offset
        | if firmware_cached { 0 } else { META_UNCACHED }
        | if gpu_cached { 0 } else { SLC_UNCACHED })
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::meta_fwif_address;

    #[ktest]
    fn meta_fwif_address_encodes_selected_config_slots_and_rejects_outside_heap() {
        // RockOS RGXSetFirmwareAddress: config connection is uncached in META,
        // while OSINIT and SYSINIT request FIRMWARE_CACHED; all are GPU_UNCACHED.
        assert_eq!(
            meta_fwif_address(0xe1c1_fd0000, false, false),
            Ok(0xf1fd_0000)
        );
        assert_eq!(
            meta_fwif_address(0xe1c1_fe0000, true, false),
            Ok(0x71fe_0000)
        );
        assert_eq!(
            meta_fwif_address(0xe1c1_ff0000, true, false),
            Ok(0x71ff_0000)
        );
        assert_eq!(
            meta_fwif_address(0xe1c0_040000, false, false),
            Ok(0xf004_0000)
        );
        assert_eq!(
            meta_fwif_address(0xe1c0_040000, true, true),
            Ok(0x1004_0000)
        );
        assert!(meta_fwif_address(0xe1bf_ffffff, false, false).is_err());
        assert!(meta_fwif_address(0xe1c2_000000, false, false).is_err());
        assert!(meta_fwif_address(0xe1c0_000001, false, false).is_err());
    }
}
