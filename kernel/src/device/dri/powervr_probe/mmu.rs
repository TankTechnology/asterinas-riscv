// SPDX-License-Identifier: MPL-2.0

//! PowerVR MMUv4 preparation for the selected Megrez firmware session.

use alloc::vec::Vec;

use ostd::mm::PAGE_SIZE;

use super::dma::GpuDmaAllocation;

// RockOS bf2ec5d53002c16bc1bc593b92516eb6c2866176, img-volcanic:
// hwdefs/volcanic/km/rgxmmudefs_km.h and rgxmmuinit.c. The BVNC
// The 30.0.408.101 feature row for BVNC 30.3.408.101 selects MMU_VERSION=4
// and has MH_PARITY=0.
const ADDRESS_LIMIT: usize = 1 << 40;
const AXCACHE_WBRWALLOC: u64 = 0x3c00_0000_0000_0000;

fn check_page_address(addr: usize) -> Result<(), &'static str> {
    if addr == 0 || addr >= ADDRESS_LIMIT || !addr.is_multiple_of(PAGE_SIZE) {
        return Err("gpu_mmu_invalid_page_address");
    }
    Ok(())
}

fn validate_map(virt: usize, phys: usize, pages: usize) -> Result<(), &'static str> {
    check_page_address(virt)?;
    check_page_address(phys)?;
    let bytes = pages
        .checked_mul(PAGE_SIZE)
        .ok_or("gpu_mmu_invalid_map_size")?;
    if bytes == 0
        || virt
            .checked_add(bytes)
            .is_none_or(|end| end > ADDRESS_LIMIT)
        || phys
            .checked_add(bytes)
            .is_none_or(|end| end > ADDRESS_LIMIT)
    {
        return Err("gpu_mmu_invalid_map_size");
    }
    Ok(())
}

fn page_indices(virt: usize) -> Result<(usize, usize, usize), &'static str> {
    check_page_address(virt)?;
    Ok((
        (virt >> 30) & 0x3ff,
        (virt >> 21) & 0x1ff,
        (virt >> 12) & 0x1ff,
    ))
}

fn encode_pc_entry(pd_phys: usize) -> Result<u32, &'static str> {
    check_page_address(pd_phys)?;
    Ok(((pd_phys >> 12) << 4) as u32 | 1)
}

fn encode_pd_entry(pt_phys: usize) -> Result<u64, &'static str> {
    check_page_address(pt_phys)?;
    // MMUv4 has no page-size field in the page-directory entry.
    Ok(pt_phys as u64 | 1)
}

fn encode_pt_entry(
    page_phys: usize,
    read_only: bool,
    pm_meta_protect: bool,
) -> Result<u64, &'static str> {
    check_page_address(page_phys)?;
    Ok(AXCACHE_WBRWALLOC
        | (u64::from(pm_meta_protect) << 62)
        | page_phys as u64
        | (u64::from(read_only) << 1)
        | 1)
}

struct PageTable {
    pd_index: usize,
    page: GpuDmaAllocation,
}

struct PageDirectory {
    pc_index: usize,
    page: GpuDmaAllocation,
    tables: Vec<PageTable>,
}

struct OwnedMap {
    start: usize,
    end: usize,
    _allocation: GpuDmaAllocation,
}

#[derive(Debug, Eq, PartialEq)]
pub(super) struct GpuMmuMapReceipt {
    pub(super) root_daddr: usize,
    pub(super) pages: usize,
    pub(super) table_pages: usize,
    pub(super) first_pte: u64,
    pub(super) last_pte: u64,
}

/// Owns the DMA pages referenced by the GPU page tables. The root is not
/// installed in any GPU register until the firmware boot path is complete.
pub(super) struct GpuMmu4 {
    root: GpuDmaAllocation,
    directories: Vec<PageDirectory>,
    mapped: Vec<OwnedMap>,
    poisoned: bool,
}

impl GpuMmu4 {
    pub(super) fn new() -> Result<Self, &'static str> {
        Ok(Self {
            root: GpuDmaAllocation::new(1)?,
            directories: Vec::new(),
            mapped: Vec::new(),
            poisoned: false,
        })
    }

    pub(super) fn root_daddr(&self) -> usize {
        self.root.daddr()
    }

    #[cfg(ktest)]
    pub(super) fn test_pte(&self, virt: usize) -> Result<u64, &'static str> {
        let (pc_index, pd_index, pt_index) = page_indices(virt)?;
        let Some(directory) = self
            .directories
            .iter()
            .find(|directory| directory.pc_index == pc_index)
        else {
            return Ok(0);
        };
        let Some(table) = directory
            .tables
            .iter()
            .find(|table| table.pd_index == pd_index)
        else {
            return Ok(0);
        };
        table.page.read_u64(pt_index * 8)
    }

    pub(super) fn map_owned(
        &mut self,
        virt: usize,
        allocation: GpuDmaAllocation,
        read_only: bool,
        pm_meta_protect: bool,
    ) -> Result<GpuMmuMapReceipt, &'static str> {
        if self.poisoned {
            return Err("gpu_mmu_table_poisoned");
        }
        let pages = allocation.size() / PAGE_SIZE;
        validate_map(virt, allocation.daddr(), pages)?;
        let end = virt + allocation.size();
        if self
            .mapped
            .iter()
            .any(|mapped| virt < mapped.end && mapped.start < end)
        {
            return Err("gpu_mmu_mapping_overlap");
        }

        let result = (|| {
            let mut first_pte = 0;
            let mut last_pte = 0;
            for page_number in 0..pages {
                let page_virt = virt + page_number * PAGE_SIZE;
                let page_phys = allocation.daddr() + page_number * PAGE_SIZE;
                let (pc_index, pd_index, pt_index) = page_indices(page_virt)?;

                let directory_index = match self
                    .directories
                    .iter()
                    .position(|directory| directory.pc_index == pc_index)
                {
                    Some(index) => index,
                    None => {
                        let page = GpuDmaAllocation::new(1)?;
                        let entry = encode_pc_entry(page.daddr())?;
                        self.root.write_u32(pc_index * 4, entry)?;
                        if self.root.read_u32(pc_index * 4)? != entry {
                            return Err("gpu_mmu_pc_readback_mismatch");
                        }
                        self.directories.push(PageDirectory {
                            pc_index,
                            page,
                            tables: Vec::new(),
                        });
                        self.directories.len() - 1
                    }
                };
                let directory = &mut self.directories[directory_index];
                let table_index = match directory
                    .tables
                    .iter()
                    .position(|table| table.pd_index == pd_index)
                {
                    Some(index) => index,
                    None => {
                        let page = GpuDmaAllocation::new(1)?;
                        let entry = encode_pd_entry(page.daddr())?;
                        directory.page.write_u64(pd_index * 8, entry)?;
                        if directory.page.read_u64(pd_index * 8)? != entry {
                            return Err("gpu_mmu_pd_readback_mismatch");
                        }
                        directory.tables.push(PageTable { pd_index, page });
                        directory.tables.len() - 1
                    }
                };
                let table = &directory.tables[table_index];
                if table.page.read_u64(pt_index * 8)? != 0 {
                    return Err("gpu_mmu_mapping_overlap");
                }
                let entry = encode_pt_entry(page_phys, read_only, pm_meta_protect)?;
                table.page.write_u64(pt_index * 8, entry)?;
                let observed = table.page.read_u64(pt_index * 8)?;
                if observed != entry {
                    return Err("gpu_mmu_pt_readback_mismatch");
                }
                if page_number == 0 {
                    first_pte = observed;
                }
                last_pte = observed;
            }
            let table_pages = 1
                + self.directories.len()
                + self
                    .directories
                    .iter()
                    .map(|directory| directory.tables.len())
                    .sum::<usize>();
            Ok(GpuMmuMapReceipt {
                root_daddr: self.root_daddr(),
                pages,
                table_pages,
                first_pte,
                last_pte,
            })
        })();
        match result {
            Ok(receipt) => {
                self.mapped.push(OwnedMap {
                    start: virt,
                    end,
                    _allocation: allocation,
                });
                Ok(receipt)
            }
            Err(error) => {
                // Partial tables must never be installed after an allocation or
                // readback error. Reject every later use of this root.
                self.poisoned = true;
                Err(error)
            }
        }
    }
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::{
        GpuMmu4, encode_pc_entry, encode_pd_entry, encode_pt_entry, page_indices, validate_map,
    };
    use crate::device::dri::powervr_probe::dma::GpuDmaAllocation;

    #[ktest]
    fn gpu_mmu4_maps_only_an_owned_dma_allocation_across_table_boundary() {
        let mut mmu = GpuMmu4::new().unwrap();
        let allocation = GpuDmaAllocation::new(2).unwrap();
        let first_phys = allocation.daddr();
        let virt = 0xe1c0_1ff000;
        let receipt = mmu.map_owned(virt, allocation, false, true).unwrap();
        assert_eq!(receipt.root_daddr, mmu.root_daddr());
        assert_eq!(receipt.pages, 2);
        assert_eq!(receipt.table_pages, 4);
        assert_eq!(
            receipt.first_pte,
            encode_pt_entry(first_phys, false, true).unwrap()
        );
        assert_eq!(
            receipt.last_pte,
            encode_pt_entry(first_phys + 4096, false, true).unwrap()
        );
        assert_eq!(
            mmu.map_owned(virt, GpuDmaAllocation::new(1).unwrap(), false, true),
            Err("gpu_mmu_mapping_overlap")
        );
    }

    #[ktest]
    fn gpu_mmu4_encodes_rockos_page_table_entries() {
        assert_eq!(encode_pc_entry(0x9000_0000), Ok(0x0090_0001));
        assert_eq!(encode_pd_entry(0x9100_0000), Ok(0x9100_0001));
        assert_eq!(
            encode_pt_entry(0x9200_0000, false, false),
            Ok(0x3c00_0000_9200_0001)
        );
        assert_eq!(
            encode_pt_entry(0x9200_0000, true, false),
            Ok(0x3c00_0000_9200_0003)
        );
        assert_eq!(
            encode_pt_entry(0x9200_0000, false, true),
            Ok(0x7c00_0000_9200_0001)
        );
    }

    #[ktest]
    fn gpu_mmu4_rejects_unaligned_and_out_of_range_addresses() {
        assert!(encode_pc_entry(0x9000_0001).is_err());
        assert!(encode_pd_entry(1 << 40).is_err());
        assert!(encode_pt_entry(1 << 40, false, false).is_err());
        assert!(validate_map(0x4000_0001, 0x9000_0000, 1).is_err());
        assert!(validate_map(0x4000_0000, 0x9000_0001, 1).is_err());
        assert!(validate_map((1 << 40) - 4096, 0x9000_0000, 2).is_err());
        assert!(validate_map(0x4000_0000, 0x9000_0000, 0).is_err());
    }

    #[ktest]
    fn gpu_mmu4_page_indices_cross_pt_and_pd_boundaries() {
        assert_eq!(page_indices(0x001f_f000), Ok((0, 0, 511)));
        assert_eq!(page_indices(0x0020_0000), Ok((0, 1, 0)));
        assert_eq!(page_indices(0x3fff_f000), Ok((0, 511, 511)));
        assert_eq!(page_indices(0x4000_0000), Ok((1, 0, 0)));
        assert!(page_indices(1 << 40).is_err());
    }
}
