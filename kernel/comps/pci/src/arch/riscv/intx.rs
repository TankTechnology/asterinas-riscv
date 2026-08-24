// SPDX-License-Identifier: MPL-2.0

use ostd::mm::dma::DmaWindow;

use crate::PciDeviceLocation;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct PciIntxRoute {
    pub(super) interrupt_parent: u32,
    pub(super) interrupt: u32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct PciIntxEndpoint {
    pub(super) location: PciDeviceLocation,
    pub(super) pin: u8,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct ParentInterruptSpec {
    pub(super) address_cells: usize,
    pub(super) interrupt_cells: usize,
}

#[derive(Clone, Copy)]
pub(super) struct HostDmaFields<'a> {
    pub(super) dma_coherent: bool,
    pub(super) dma_ranges: Option<&'a [u8]>,
    pub(super) has_iommu: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RiscvPciResourceError {
    MissingHost,
    NonCoherentDma,
    DmaTranslation,
    Iommu,
    MissingIntx,
    InvalidIntxMap,
    SharedIntx,
}

pub(super) fn validate_dma_contract(
    fields: HostDmaFields<'_>,
) -> Result<DmaWindow, RiscvPciResourceError> {
    if !fields.dma_coherent {
        return Err(RiscvPciResourceError::NonCoherentDma);
    }
    if fields.has_iommu {
        return Err(RiscvPciResourceError::Iommu);
    }
    if fields.dma_ranges.is_some_and(|ranges| !ranges.is_empty()) {
        return Err(RiscvPciResourceError::DmaTranslation);
    }
    Ok(DmaWindow::new(0, 0, usize::MAX).unwrap())
}

fn read_cell(bytes: &[u8], index: usize) -> Option<u32> {
    let start = index.checked_mul(size_of::<u32>())?;
    Some(u32::from_be_bytes(
        bytes
            .get(start..start + size_of::<u32>())?
            .try_into()
            .ok()?,
    ))
}

pub(super) fn resolve_intx_cells(
    location: PciDeviceLocation,
    pin: u8,
    mask: &[u8],
    map: &[u8],
    mut parent_spec: impl FnMut(u32) -> Option<ParentInterruptSpec>,
) -> Result<PciIntxRoute, RiscvPciResourceError> {
    const CHILD_CELLS: usize = 4;
    if !(1..=4).contains(&pin)
        || mask.len() != CHILD_CELLS * size_of::<u32>()
        || map.is_empty()
        || !map.len().is_multiple_of(size_of::<u32>())
    {
        return Err(if (1..=4).contains(&pin) {
            RiscvPciResourceError::InvalidIntxMap
        } else {
            RiscvPciResourceError::MissingIntx
        });
    }

    let target = [
        (u32::from(location.bus) << 16)
            | (u32::from(location.device) << 11)
            | (u32::from(location.function) << 8),
        0,
        0,
        u32::from(pin),
    ];
    let masks = [
        read_cell(mask, 0).unwrap(),
        read_cell(mask, 1).unwrap(),
        read_cell(mask, 2).unwrap(),
        read_cell(mask, 3).unwrap(),
    ];

    let total_cells = map.len() / size_of::<u32>();
    let mut cursor = 0usize;
    let mut matched = None;
    while cursor < total_cells {
        let parent_index = cursor
            .checked_add(CHILD_CELLS)
            .ok_or(RiscvPciResourceError::InvalidIntxMap)?;
        let parent = read_cell(map, parent_index).ok_or(RiscvPciResourceError::InvalidIntxMap)?;
        let spec = parent_spec(parent).ok_or(RiscvPciResourceError::InvalidIntxMap)?;
        if spec.interrupt_cells != 1 || spec.address_cells > 4 {
            return Err(RiscvPciResourceError::InvalidIntxMap);
        }
        let entry_cells = CHILD_CELLS
            .checked_add(1)
            .and_then(|cells| cells.checked_add(spec.address_cells))
            .and_then(|cells| cells.checked_add(spec.interrupt_cells))
            .ok_or(RiscvPciResourceError::InvalidIntxMap)?;
        let next = cursor
            .checked_add(entry_cells)
            .filter(|next| *next <= total_cells)
            .ok_or(RiscvPciResourceError::InvalidIntxMap)?;

        let child_matches = (0..CHILD_CELLS).all(|index| {
            read_cell(map, cursor + index)
                .is_some_and(|value| value & masks[index] == target[index] & masks[index])
        });
        if child_matches {
            let interrupt_index = parent_index + 1 + spec.address_cells;
            let route = PciIntxRoute {
                interrupt_parent: parent,
                interrupt: read_cell(map, interrupt_index)
                    .ok_or(RiscvPciResourceError::InvalidIntxMap)?,
            };
            if matched.replace(route).is_some() {
                return Err(RiscvPciResourceError::InvalidIntxMap);
            }
        }
        cursor = next;
    }
    matched.ok_or(RiscvPciResourceError::MissingIntx)
}

pub(super) fn require_exclusive_route(
    target: PciIntxEndpoint,
    endpoints: impl IntoIterator<Item = PciIntxEndpoint>,
    mut resolve: impl FnMut(PciDeviceLocation, u8) -> Result<PciIntxRoute, RiscvPciResourceError>,
) -> Result<PciIntxRoute, RiscvPciResourceError> {
    let target_route = resolve(target.location, target.pin)?;
    let mut matches = 0usize;
    let mut target_present = false;
    for endpoint in endpoints {
        target_present |= endpoint == target;
        if resolve(endpoint.location, endpoint.pin)? == target_route {
            matches += 1;
        }
    }
    if target_present && matches == 1 {
        Ok(target_route)
    } else {
        Err(RiscvPciResourceError::SharedIntx)
    }
}

#[cfg(ktest)]
mod tests {
    use alloc::vec::Vec;

    use ostd::prelude::ktest;

    use super::*;

    const QEMU_MASK: [u32; 4] = [0x1800, 0, 0, 0x7];
    const QEMU_MAP: [u32; 12] = [
        0, 0, 0, 1, 9, 32, // slot 0 INTA -> PLIC 32
        0x800, 0, 0, 1, 9, 33, // slot 1 INTA -> PLIC 33
    ];

    fn bdf(device: u8) -> PciDeviceLocation {
        PciDeviceLocation {
            bus: 0,
            device,
            function: 0,
        }
    }

    fn cells(values: &[u32]) -> Vec<u8> {
        values
            .iter()
            .flat_map(|value| value.to_be_bytes())
            .collect()
    }

    fn qemu_parent(phandle: u32) -> Option<ParentInterruptSpec> {
        (phandle == 9).then_some(ParentInterruptSpec {
            address_cells: 0,
            interrupt_cells: 1,
        })
    }

    fn qemu_route(
        location: PciDeviceLocation,
        pin: u8,
    ) -> Result<PciIntxRoute, RiscvPciResourceError> {
        resolve_intx_cells(
            location,
            pin,
            &cells(&QEMU_MASK),
            &cells(&QEMU_MAP),
            qemu_parent,
        )
    }

    #[ktest]
    fn riscv_pci_contract_qemu_slot_one_inta_resolves_to_plic_33() {
        assert_eq!(
            qemu_route(bdf(1), 1),
            Ok(PciIntxRoute {
                interrupt_parent: 9,
                interrupt: 33,
            })
        );
    }

    #[ktest]
    fn riscv_pci_contract_address_and_pin_both_select_the_interrupt_route() {
        assert_eq!(qemu_route(bdf(0), 1).unwrap().interrupt, 32);
        assert_eq!(qemu_route(bdf(1), 1).unwrap().interrupt, 33);
        assert_eq!(
            qemu_route(bdf(1), 0),
            Err(RiscvPciResourceError::MissingIntx)
        );
        assert_eq!(
            qemu_route(bdf(1), 5),
            Err(RiscvPciResourceError::MissingIntx)
        );
    }

    #[ktest]
    fn riscv_pci_contract_malformed_or_ambiguous_maps_fail_closed() {
        assert_eq!(
            resolve_intx_cells(bdf(1), 1, &[0; 15], &cells(&QEMU_MAP), qemu_parent),
            Err(RiscvPciResourceError::InvalidIntxMap)
        );
        let truncated = cells(&QEMU_MAP);
        assert_eq!(
            resolve_intx_cells(
                bdf(1),
                1,
                &cells(&QEMU_MASK),
                &truncated[..truncated.len() - 1],
                qemu_parent,
            ),
            Err(RiscvPciResourceError::InvalidIntxMap)
        );

        let mut duplicate = QEMU_MAP.to_vec();
        duplicate.extend_from_slice(&QEMU_MAP[6..]);
        assert_eq!(
            resolve_intx_cells(
                bdf(1),
                1,
                &cells(&QEMU_MASK),
                &cells(&duplicate),
                qemu_parent,
            ),
            Err(RiscvPciResourceError::InvalidIntxMap)
        );

        assert_eq!(
            resolve_intx_cells(bdf(1), 1, &cells(&QEMU_MASK), &cells(&QEMU_MAP), |_| None,),
            Err(RiscvPciResourceError::InvalidIntxMap)
        );
    }

    #[ktest]
    fn riscv_pci_contract_route_must_be_unique_among_present_functions() {
        let target = PciIntxEndpoint {
            location: bdf(1),
            pin: 1,
        };
        assert_eq!(
            require_exclusive_route(target, [target], qemu_route),
            qemu_route(target.location, target.pin)
        );
        assert_eq!(
            require_exclusive_route(
                target,
                [
                    target,
                    PciIntxEndpoint {
                        location: bdf(5),
                        pin: 1,
                    },
                ],
                qemu_route,
            ),
            Err(RiscvPciResourceError::SharedIntx)
        );
        assert_eq!(
            require_exclusive_route(
                target,
                [PciIntxEndpoint {
                    location: bdf(5),
                    pin: 1,
                }],
                qemu_route,
            ),
            Err(RiscvPciResourceError::SharedIntx)
        );
    }

    #[ktest]
    fn riscv_pci_contract_dma_accepts_only_coherent_identity_access() {
        let valid = HostDmaFields {
            dma_coherent: true,
            dma_ranges: None,
            has_iommu: false,
        };
        assert_eq!(
            validate_dma_contract(valid),
            Ok(DmaWindow::new(0, 0, usize::MAX).unwrap())
        );
        assert_eq!(
            validate_dma_contract(HostDmaFields {
                dma_coherent: false,
                ..valid
            }),
            Err(RiscvPciResourceError::NonCoherentDma)
        );
        assert_eq!(
            validate_dma_contract(HostDmaFields {
                dma_ranges: Some(&[]),
                ..valid
            }),
            Ok(DmaWindow::new(0, 0, usize::MAX).unwrap())
        );
        assert_eq!(
            validate_dma_contract(HostDmaFields {
                dma_ranges: Some(&[0; 24]),
                ..valid
            }),
            Err(RiscvPciResourceError::DmaTranslation)
        );
        assert_eq!(
            validate_dma_contract(HostDmaFields {
                has_iommu: true,
                ..valid
            }),
            Err(RiscvPciResourceError::Iommu)
        );
    }
}
