// SPDX-License-Identifier: MPL-2.0

// Compile with the selected RockOS config_kernel.h and Volcanic headers.
// The ELF section exposes vendor register offsets and reset masks without
// executing GPU code or depending on the host's integer printf ABI.
#include "configs/rgxconfig_km_30.V.408.101.h"
#include "rgx_fwif_hwperf.h"
#include "rgxheapconfig.h"
#define IMG_EXPLICIT_INCLUDE_HWDEFS
#include "rgx_cr_defs_km.h"
#undef IMG_EXPLICIT_INCLUDE_HWDEFS

struct rgx_register_entry {
    char name[64];
    unsigned long long value;
};

_Static_assert(sizeof(struct rgx_register_entry) == 72, "unexpected probe entry");

#define VALUE(name) { #name, name }

__attribute__((used, section(".rgx_fwif_abi")))
const struct rgx_register_entry rgx_start_registers[] = {
    VALUE(RGX_CR_SYS_BUS_SECURE),
    VALUE(RGX_CR_MERCER_SOFT_RESET),
    VALUE(RGX_CR_SWIFT_SOFT_RESET),
    VALUE(RGX_CR_TEXAS_SOFT_RESET),
    VALUE(RGX_CR_SOFT_RESET),
    VALUE(RGX_CR_META_BOOT),
    VALUE(RGX_CR_MTS_GARTEN_WRAPPER_CONFIG),
    VALUE(RGX_CR_MMU_PAGE_SIZE_RANGE_ONE),
    VALUE(RGX_CR_ACE_CTRL),
    VALUE(RGX_CR_MERCER0_SOFT_RESET_SPU_EN),
    VALUE(RGX_CR_MERCER1_SOFT_RESET_SPU_EN),
    VALUE(RGX_CR_MERCER2_SOFT_RESET_SPU_EN),
    VALUE(RGX_CR_MERCER_SOFT_RESET_MASKFULL),
    VALUE(RGX_CR_SWIFT_SOFT_RESET_MASKFULL),
    VALUE(RGX_CR_TEXAS_SOFT_RESET_MASKFULL),
    VALUE(RGX_SOFT_RESET_JONES_ALL),
    VALUE(RGX_SOFT_RESET_EXTRA),
    VALUE(RGX_CR_SOFT_RESET_GARTEN_EN),
    VALUE(RGX_CR_META_BOOT_MODE_EN),
    VALUE(RGX_CR_MTS_GARTEN_WRAPPER_CONFIG_IDLE_CTRL_META),
    VALUE(RGX_CR_ACE_CTRL_MMU_AWCACHE_WRITE_BACK_WRITE_ALLOCATE),
    VALUE(RGX_CR_ACE_CTRL_MMU_ARCACHE_WRITE_BACK_READ_ALLOCATE),
    VALUE(RGX_CR_ACE_CTRL_PM_MMU_AXCACHE_SHIFT),
    VALUE(RGX_CR_ACE_CTRL_COH_DOMAIN_OUTER_SHAREABLE),
    VALUE(RGX_CR_ACE_CTRL_NON_COH_DOMAIN_NON_SHAREABLE),
    VALUE(RGX_CR_MMU_PAGE_SIZE_RANGE_ONE_END_ADDR_SHIFT),
    VALUE(RGX_CR_MMU_PAGE_SIZE_RANGE_ONE_END_ADDR_ALIGNSHIFT),
    { "selected_ace_ctrl", RGX_CR_ACE_CTRL_MMU_AWCACHE_WRITE_BACK_WRITE_ALLOCATE |
        RGX_CR_ACE_CTRL_MMU_ARCACHE_WRITE_BACK_READ_ALLOCATE |
        ((unsigned long long)0xf << RGX_CR_ACE_CTRL_PM_MMU_AXCACHE_SHIFT) |
        RGX_CR_ACE_CTRL_COH_DOMAIN_OUTER_SHAREABLE |
        RGX_CR_ACE_CTRL_NON_COH_DOMAIN_NON_SHAREABLE },
    { "selected_mmu_global_4k", (((1ULL << 40) - (1ULL << 21)) >>
        RGX_CR_MMU_PAGE_SIZE_RANGE_ONE_END_ADDR_ALIGNSHIFT) <<
        RGX_CR_MMU_PAGE_SIZE_RANGE_ONE_END_ADDR_SHIFT },
    { "selected_mmu_non4k_16k", (1ULL << 38) |
        (((RGX_GENERAL_NON4K_HEAP_BASE + RGX_GENERAL_NON4K_HEAP_SIZE -
           (1ULL << 21)) >> 21) << 19) |
        (RGX_GENERAL_NON4K_HEAP_BASE >> 21) },
};
