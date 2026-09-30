// SPDX-License-Identifier: MPL-2.0

//! Selected Megrez META firmware-interface address contract.

use super::dma::{FW_HEAP_BASE, FW_RAW_HEAP_SIZE};

// RockOS bf2ec5d5 rgxfwutils.c:RGXSetFirmwareAddress and rgx_meta.h.
const META_DATA_BASE: u32 = 0x1000_0000;
const META_UNCACHED: u32 = 0x8000_0000;
const SLC_UNCACHED: u32 = 0x6000_0000;

/// First system objects referenced by the selected RockOS SYSINIT layout.
/// GPU page-table read-only and CPU access are independent permissions.
pub(super) struct SystemObject {
    pub(super) bytes: usize,
    pub(super) sysinit_offset: usize,
    pub(super) firmware_cached: bool,
    pub(super) gpu_cached: bool,
    pub(super) gpu_read_only: bool,
}

pub(super) const SYSTEM_OBJECTS: [SystemObject; 4] = [
    SystemObject {
        bytes: 864, // RGXFWIF_TRACEBUF
        sysinit_offset: 164,
        firmware_cached: false,
        gpu_cached: false,
        gpu_read_only: false,
    },
    SystemObject {
        bytes: 3656, // RGXFWIF_SYSDATA
        sysinit_offset: 168,
        firmware_cached: false,
        gpu_cached: false,
        gpu_read_only: false,
    },
    SystemObject {
        bytes: 11824, // RGXFWIF_GPU_UTIL_FW
        sysinit_offset: 172,
        firmware_cached: true,
        gpu_cached: false,
        gpu_read_only: false,
    },
    SystemObject {
        bytes: 184, // RGXFWIF_RUNTIME_CFG
        sysinit_offset: 160,
        firmware_cached: false,
        gpu_cached: false,
        gpu_read_only: true,
    },
];

/// OSINIT-owned firmware objects. The selected RockOS config uses 2^10 kernel
/// commands; the firmware CCB has 2^5 commands without PDVFS/workload support.
const KCCB_COMMANDS_LOG2: usize = 10;
const FWCCB_COMMANDS_LOG2: usize = 5;
pub(super) struct OsObject {
    pub(super) bytes: usize,
    pub(super) osinit_offset: usize,
    pub(super) firmware_cached: bool,
    pub(super) gpu_read_only: bool,
    pub(super) ccb_wrap_mask: Option<u32>,
}

pub(super) const OS_OBJECTS: [OsObject; 7] = [
    OsObject {
        bytes: 2464, // RGXFWIF_HWRINFOBUF
        osinit_offset: 28,
        firmware_cached: false,
        gpu_read_only: false,
        ccb_wrap_mask: None,
    },
    OsObject {
        bytes: 16, // Kernel CCB control
        osinit_offset: 0,
        firmware_cached: false,
        gpu_read_only: false,
        ccb_wrap_mask: Some((1 << KCCB_COMMANDS_LOG2) - 1),
    },
    OsObject {
        bytes: (1 << KCCB_COMMANDS_LOG2) * 64, // Kernel CCB commands
        osinit_offset: 4,
        firmware_cached: true,
        gpu_read_only: true,
        ccb_wrap_mask: None,
    },
    OsObject {
        bytes: (1 << KCCB_COMMANDS_LOG2) * 4, // Kernel CCB return slots
        osinit_offset: 8,
        firmware_cached: false,
        gpu_read_only: false,
        ccb_wrap_mask: None,
    },
    OsObject {
        bytes: 16, // Firmware CCB control
        osinit_offset: 12,
        firmware_cached: false,
        gpu_read_only: false,
        ccb_wrap_mask: Some((1 << FWCCB_COMMANDS_LOG2) - 1),
    },
    OsObject {
        bytes: (1 << FWCCB_COMMANDS_LOG2) * 64, // Firmware CCB commands
        osinit_offset: 16,
        firmware_cached: false,
        gpu_read_only: false,
        ccb_wrap_mask: None,
    },
    OsObject {
        bytes: 1096, // RGXFWIF_OSDATA
        osinit_offset: 36,
        firmware_cached: false,
        gpu_read_only: false,
        ccb_wrap_mask: None,
    },
];

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
