// SPDX-License-Identifier: MPL-2.0

use ostd::{
    io::IoMem,
    mm::{HasSize, VmIoOnce},
};

const CAPLENGTH_HCIVERSION: usize = 0x00;
const HCSPARAMS1: usize = 0x04;
const HCCPARAMS1: usize = 0x10;
const DBOFF: usize = 0x14;
const RTSOFF: usize = 0x18;
const MIN_CAPABILITY_LENGTH: usize = 0x20;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct XhciCapabilities {
    pub version: u16,
    pub max_slots: u8,
    pub max_interrupters: u16,
    pub max_ports: u8,
    pub addresses_64bit: bool,
    pub contexts_64byte: bool,
    pub operational_offset: usize,
    pub runtime_offset: usize,
    pub doorbell_offset: usize,
    pub extended_capabilities_offset: Option<usize>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum CapabilityError {
    MmioRead,
    InvalidCapabilityLength,
    UnsupportedVersion,
    InvalidTopology,
    InvalidRegisterOffset,
}

#[derive(Clone, Copy)]
struct RawCapabilities {
    caplength_hciversion: u32,
    hcsparams1: u32,
    hccparams1: u32,
    dboff: u32,
    rtsoff: u32,
}

fn decode(raw: RawCapabilities, mmio_size: usize) -> Result<XhciCapabilities, CapabilityError> {
    let operational_offset = (raw.caplength_hciversion & 0xff) as usize;
    if operational_offset < MIN_CAPABILITY_LENGTH
        || !operational_offset.is_multiple_of(size_of::<u32>())
        || operational_offset >= mmio_size
    {
        return Err(CapabilityError::InvalidCapabilityLength);
    }

    let version = (raw.caplength_hciversion >> 16) as u16;
    if !(0x0090..=0x0120).contains(&version) {
        return Err(CapabilityError::UnsupportedVersion);
    }

    let max_slots = (raw.hcsparams1 & 0xff) as u8;
    let max_interrupters = ((raw.hcsparams1 >> 8) & 0x7ff) as u16;
    let max_ports = (raw.hcsparams1 >> 24) as u8;
    if max_slots == 0 || max_interrupters == 0 || max_ports == 0 {
        return Err(CapabilityError::InvalidTopology);
    }

    let doorbell_offset = (raw.dboff as usize) & !0x3;
    let runtime_offset = (raw.rtsoff as usize) & !0x1f;
    let extended_capabilities_offset = match (raw.hccparams1 >> 16) as usize {
        0 => None,
        dword_offset => dword_offset.checked_mul(size_of::<u32>()),
    };
    if !region_fits(
        operational_offset,
        0x400 + usize::from(max_ports) * 0x10,
        mmio_size,
    ) || doorbell_offset < operational_offset
        || !region_fits(
            doorbell_offset,
            (usize::from(max_slots) + 1) * size_of::<u32>(),
            mmio_size,
        )
        || runtime_offset < operational_offset
        || !region_fits(
            runtime_offset,
            0x20 + usize::from(max_interrupters) * 0x20,
            mmio_size,
        )
        || extended_capabilities_offset.is_some_and(|offset| {
            offset < operational_offset || !region_fits(offset, size_of::<u32>(), mmio_size)
        })
    {
        return Err(CapabilityError::InvalidRegisterOffset);
    }

    Ok(XhciCapabilities {
        version,
        max_slots,
        max_interrupters,
        max_ports,
        addresses_64bit: raw.hccparams1 & 1 != 0,
        contexts_64byte: raw.hccparams1 & (1 << 2) != 0,
        operational_offset,
        runtime_offset,
        doorbell_offset,
        extended_capabilities_offset,
    })
}

fn region_fits(offset: usize, length: usize, mmio_size: usize) -> bool {
    offset
        .checked_add(length)
        .is_some_and(|end| end <= mmio_size)
}

fn probe_with(
    mmio_size: usize,
    mut read: impl FnMut(usize) -> Result<u32, CapabilityError>,
) -> Result<XhciCapabilities, CapabilityError> {
    let raw = RawCapabilities {
        caplength_hciversion: read(CAPLENGTH_HCIVERSION)?,
        hcsparams1: read(HCSPARAMS1)?,
        hccparams1: read(HCCPARAMS1)?,
        dboff: read(DBOFF)?,
        rtsoff: read(RTSOFF)?,
    };

    decode(raw, mmio_size)
}

pub(super) fn probe(mmio: &IoMem) -> Result<XhciCapabilities, CapabilityError> {
    probe_with(mmio.size(), |offset| {
        mmio.read_once::<u32>(offset)
            .map_err(|_| CapabilityError::MmioRead)
    })
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::*;

    use super::*;

    fn valid_raw() -> RawCapabilities {
        RawCapabilities {
            caplength_hciversion: 0x0110_0020,
            hcsparams1: 0x0200_0104,
            hccparams1: 0x0040_0005,
            dboff: 0x2000,
            rtsoff: 0x1000,
        }
    }

    #[ktest]
    fn reads_capabilities_from_supplied_mapping() {
        let mut offsets = [usize::MAX; 5];
        let mut read_count = 0;
        let capabilities = probe_with(0x1_0000, |offset| {
            offsets[read_count] = offset;
            read_count += 1;
            Ok(match offset {
                CAPLENGTH_HCIVERSION => 0x0110_0020,
                HCSPARAMS1 => 0x0200_0104,
                HCCPARAMS1 => 0x0040_0005,
                DBOFF => 0x2000,
                RTSOFF => 0x1000,
                _ => unreachable!(),
            })
        })
        .unwrap();

        assert_eq!(capabilities.version, 0x0110);
        assert_eq!(
            offsets,
            [CAPLENGTH_HCIVERSION, HCSPARAMS1, HCCPARAMS1, DBOFF, RTSOFF,]
        );
    }

    #[ktest]
    fn decodes_bounded_xhci_capabilities() {
        let capabilities = decode(valid_raw(), 0x1_0000).unwrap();

        assert_eq!(capabilities.version, 0x0110);
        assert_eq!(capabilities.max_slots, 4);
        assert_eq!(capabilities.max_interrupters, 1);
        assert_eq!(capabilities.max_ports, 2);
        assert!(capabilities.addresses_64bit);
        assert!(capabilities.contexts_64byte);
        assert_eq!(capabilities.operational_offset, 0x20);
        assert_eq!(capabilities.runtime_offset, 0x1000);
        assert_eq!(capabilities.doorbell_offset, 0x2000);
        assert_eq!(capabilities.extended_capabilities_offset, Some(0x100));
    }

    #[ktest]
    fn rejects_offsets_outside_selected_mmio_window() {
        let mut raw = valid_raw();
        raw.dboff = 0x1_0000;
        assert_eq!(
            decode(raw, 0x1_0000),
            Err(CapabilityError::InvalidRegisterOffset)
        );

        raw = valid_raw();
        raw.hccparams1 = 0xffff_0005;
        assert_eq!(
            decode(raw, 0x1_0000),
            Err(CapabilityError::InvalidRegisterOffset)
        );
    }

    #[ktest]
    fn rejects_unknown_versions_and_empty_topology() {
        let mut raw = valid_raw();
        raw.caplength_hciversion = 0x0080_0020;
        assert_eq!(
            decode(raw, 0x1_0000),
            Err(CapabilityError::UnsupportedVersion)
        );

        raw = valid_raw();
        raw.hcsparams1 = 0;
        assert_eq!(decode(raw, 0x1_0000), Err(CapabilityError::InvalidTopology));

        raw = valid_raw();
        raw.caplength_hciversion = 0x0110_0010;
        assert_eq!(
            decode(raw, 0x1_0000),
            Err(CapabilityError::InvalidCapabilityLength)
        );
    }
}
