// SPDX-License-Identifier: MPL-2.0

// Compile against the pinned RockOS Volcanic headers and config_kernel.h.
// The named, fixed-size ELF section can be decoded without executing target
// code; this keeps the offsets tied to the RISC-V compiler's actual ABI.
#include <stddef.h>

#include "rgx_fwif_km.h"
#include "rgx_fwif_alignchecks.h"
#include "rgxheapconfig.h"

struct rgx_abi_entry {
    char name[64];
    unsigned long long value;
};

_Static_assert(sizeof(struct rgx_abi_entry) == 72, "unexpected ABI entry layout");

#define SIZE(type) { #type ".size", sizeof(type) }
#define OFFSET(type, field) { #type "." #field, offsetof(type, field) }

__attribute__((used, section(".rgx_fwif_abi")))
const struct rgx_abi_entry rgx_fwif_abi[] = {
    SIZE(RGXFWIF_DEV_VIRTADDR),
    SIZE(IMG_DEV_VIRTADDR),
    SIZE(IMG_DEV_PHYADDR),
    SIZE(RGXFWIF_CONNECTION_CTL),
    OFFSET(RGXFWIF_CONNECTION_CTL, eConnectionFwState),
    OFFSET(RGXFWIF_CONNECTION_CTL, eConnectionOsState),
    OFFSET(RGXFWIF_CONNECTION_CTL, ui32AliveFwToken),
    OFFSET(RGXFWIF_CONNECTION_CTL, ui32AliveOsToken),
    SIZE(RGXFWIF_OSINIT),
    OFFSET(RGXFWIF_OSINIT, psKernelCCBCtl),
    OFFSET(RGXFWIF_OSINIT, psKernelCCB),
    OFFSET(RGXFWIF_OSINIT, psKernelCCBRtnSlots),
    OFFSET(RGXFWIF_OSINIT, psFirmwareCCBCtl),
    OFFSET(RGXFWIF_OSINIT, psFirmwareCCB),
    OFFSET(RGXFWIF_OSINIT, psWorkEstFirmwareCCBCtl),
    OFFSET(RGXFWIF_OSINIT, psWorkEstFirmwareCCB),
    OFFSET(RGXFWIF_OSINIT, sRGXFWIfHWRInfoBufCtl),
    OFFSET(RGXFWIF_OSINIT, ui32HWRDebugDumpLimit),
    OFFSET(RGXFWIF_OSINIT, sFwOsData),
    OFFSET(RGXFWIF_OSINIT, sRGXCompChecks),
    SIZE(RGXFWIF_SYSINIT),
    OFFSET(RGXFWIF_SYSINIT, sFaultPhysAddr),
    OFFSET(RGXFWIF_SYSINIT, sPDSExecBase),
    OFFSET(RGXFWIF_SYSINIT, sUSCExecBase),
    OFFSET(RGXFWIF_SYSINIT, sFBCDCStateTableBase),
    OFFSET(RGXFWIF_SYSINIT, sFBCDCLargeStateTableBase),
    OFFSET(RGXFWIF_SYSINIT, sTextureHeapBase),
    OFFSET(RGXFWIF_SYSINIT, sPDSIndirectHeapBase),
    OFFSET(RGXFWIF_SYSINIT, ui64HWPerfFilter),
    OFFSET(RGXFWIF_SYSINIT, ui32FilterFlags),
    OFFSET(RGXFWIF_SYSINIT, sRuntimeCfg),
    OFFSET(RGXFWIF_SYSINIT, sTraceBufCtl),
    OFFSET(RGXFWIF_SYSINIT, sFwSysData),
    OFFSET(RGXFWIF_SYSINIT, sGpuUtilFWCtl),
    OFFSET(RGXFWIF_SYSINIT, sRegCfg),
    OFFSET(RGXFWIF_SYSINIT, sHWPerfCtl),
    OFFSET(RGXFWIF_SYSINIT, sCounterDumpCtl),
    OFFSET(RGXFWIF_SYSINIT, sAlignChecks),
    OFFSET(RGXFWIF_SYSINIT, ui32InitialCoreClockSpeed),
#if defined(SUPPORT_SOC_TIMER)
    OFFSET(RGXFWIF_SYSINIT, ui32InitialSOCClockSpeed),
#endif
    OFFSET(RGXFWIF_SYSINIT, ui32InitialActivePMLatencyms),
    OFFSET(RGXFWIF_SYSINIT, bFirmwareStarted),
    OFFSET(RGXFWIF_SYSINIT, ui32MarkerVal),
    OFFSET(RGXFWIF_SYSINIT, ui32FirmwareStartedTimeStamp),
    OFFSET(RGXFWIF_SYSINIT, sCorememDataStore),
    OFFSET(RGXFWIF_SYSINIT, eGPIOValidationMode),
    OFFSET(RGXFWIF_SYSINIT, sBvncKmFeatureFlags),
    SIZE(RGXFWIF_DMA_ADDR),
    OFFSET(RGXFWIF_DMA_ADDR, psDevVirtAddr),
    OFFSET(RGXFWIF_DMA_ADDR, pbyFWAddr),
    SIZE(RGXFWIF_RUNTIME_CFG),
    OFFSET(RGXFWIF_RUNTIME_CFG, ui32ActivePMLatencyms),
    OFFSET(RGXFWIF_RUNTIME_CFG, bActivePMLatencyPersistant),
    OFFSET(RGXFWIF_RUNTIME_CFG, ui32CoreClockSpeed),
#if defined(SUPPORT_SOC_TIMER)
    OFFSET(RGXFWIF_RUNTIME_CFG, ui32SOCClockSpeed),
#endif
    OFFSET(RGXFWIF_RUNTIME_CFG, ui32HCSDeadlineMS),
    OFFSET(RGXFWIF_RUNTIME_CFG, ui32PowUnitsState),
    OFFSET(RGXFWIF_RUNTIME_CFG, ui32RACUnitsState),
    OFFSET(RGXFWIF_RUNTIME_CFG, ui32WdgPeriodUs),
    OFFSET(RGXFWIF_RUNTIME_CFG, ui32TSIntervalMs),
    OFFSET(RGXFWIF_RUNTIME_CFG, ui32VzConnectionCooldownPeriodInSec),
    SIZE(RGXFWIF_TRACEBUF),
    SIZE(RGXFWIF_SYSDATA),
    SIZE(RGXFWIF_OSDATA),
    OFFSET(RGXFWIF_OSDATA, ui32FwOsConfigFlags),
    OFFSET(RGXFWIF_OSDATA, sPowerSync),
    SIZE(RGXFWIF_HWRINFOBUF),
    SIZE(RGXFWIF_CCB_CTL),
    OFFSET(RGXFWIF_CCB_CTL, ui32WriteOffset),
    OFFSET(RGXFWIF_CCB_CTL, ui32ReadOffset),
    OFFSET(RGXFWIF_CCB_CTL, ui32WrapMask),
    SIZE(RGXFWIF_KCCB_CMD),
    SIZE(RGXFWIF_FWCCB_CMD),
    SIZE(RGXFWIF_GPU_UTIL_FW),
    SIZE(RGXFWIF_REG_CFG),
    SIZE(RGXFWIF_HWPERF_CTRL),
    SIZE(RGXFWIF_COUNTER_DUMP_CTL),
    OFFSET(RGXFWIF_COUNTER_DUMP_CTL, sBuffer),
    OFFSET(RGXFWIF_COUNTER_DUMP_CTL, ui32SizeInDwords),
    OFFSET(RGXFWIF_SYSDATA, ui32ConfigFlags),
    OFFSET(RGXFWIF_SYSDATA, ui32ConfigFlagsExt),
    OFFSET(RGXFWIF_GPU_UTIL_FW, ui64GpuLastWord),
    SIZE(RGXFWIF_COMPCHECKS),
    { "RGXFW_ALIGN_CHECKS_UM_MAX", RGXFW_ALIGN_CHECKS_UM_MAX },
    { "RGX_PDSCODEDATA_HEAP_BASE", RGX_PDSCODEDATA_HEAP_BASE },
    { "RGX_USCCODE_HEAP_BASE", RGX_USCCODE_HEAP_BASE },
    { "RGX_FBCDC_HEAP_BASE", RGX_FBCDC_HEAP_BASE },
    { "RGX_FBCDC_LARGE_HEAP_BASE", RGX_FBCDC_LARGE_HEAP_BASE },
    { "RGX_TEXTURE_STATE_HEAP_BASE", RGX_TEXTURE_STATE_HEAP_BASE },
    { "RGX_PDS_INDIRECT_STATE_HEAP_BASE", RGX_PDS_INDIRECT_STATE_HEAP_BASE },
    { "RGX_HCS_DEFAULT_DEADLINE_MS", RGX_HCS_DEFAULT_DEADLINE_MS },
    { "RGXFW_SAFETY_WATCHDOG_PERIOD_IN_US", RGXFW_SAFETY_WATCHDOG_PERIOD_IN_US },
};

__attribute__((used, section(".rgx_fwif_alignchecks")))
const unsigned int rgx_fwif_alignchecks_km[] = { RGXFW_ALIGN_CHECKS_INIT_KM };
