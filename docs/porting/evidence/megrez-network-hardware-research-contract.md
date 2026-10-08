# Megrez Network Hardware Research Contract

Date: 2026-08-28

## Situation

This is **ongoing cleanup**: implementation and board results already exist,
but some hardware claims were inferred from experiments before their primary
document authority was made explicit.

## Phenomenon

- Phenomenon: Megrez GMAC1 can receive traffic and transmit initial traffic,
  but the CPU eventually stops observing TX descriptor ownership changes.
- Why it matters: the failure blocks reliable Debian networking and therefore
  browser use on Asterinas.
- Falsifier: authoritative EIC7700/DWMAC documentation or a register-level
  implementation audit showing that the selected DMA/cache mapping cannot be
  responsible for descriptor visibility.

## Unit And Universe

- Primary unit: one hardware contract claim used by the Megrez GMAC datapath.
- Universe: EIC7700 Die 0 GMAC, cache/DMA System Port, PLIC interrupt, board PHY,
  clocks/resets, and the Synopsys DWMAC descriptor/register interface.
- Include: official specifications, board documents, and version-pinned vendor
  Linux/U-Boot/DT sources that directly describe those blocks.
- Exclude: unrelated USB networking, Wi-Fi, packet-stack behavior above the
  driver, and unversioned secondary summaries.
- Known missingness: the public EIC7700X TRM and Megrez V1.1 schematic are now
  pinned by revision and SHA-256. A public Synopsys GMAC databook has not been
  located, so descriptor details not present in the EIC7700X TRM remain Level-B
  claims tied to the exact vendor Linux sources in the source ledger.

## Evidence Levels

| Level | Evidence | Allowed use |
|---|---|---|
| A | Official SoC/board/ISA/IP specification with revision | Hardware fact |
| B | Version-pinned ESWIN/Milk-V schematic, DTS, Linux, or U-Boot source | Vendor implementation contract |
| C | Asterinas code, static model, or QEMU model | Software implementation evidence |
| D | Sealed physical observation | Validation or falsification only |

A Level-D observation must not create a new hardware fact by itself. Any
unresolved conflict between levels is recorded as unknown and blocks another
board run unless that run is the smallest test that distinguishes the conflict.

## Terms

| Term | Definition | Do not confuse with |
|---|---|---|
| CPU physical address | Address used by the CPU/PMA view | DWMAC DMA/bus address |
| DMA address | Address programmed into DWMAC descriptors | Linux virtual address |
| uncached alias | EIC7700 System Port view documented as non-cacheable | PBMT_NC page attribute |
| coherent DMA | CPU/device observe writes without explicit cache maintenance | ordered descriptor publication |
| recovery | Software returns to a known U-Boot prompt | proof that the datapath passed |

## Research Questions

| RQ | Question | Required authority | Allowed claim |
|---|---|---|---|
| RQ1 | Which GMAC instance is wired to RJ45, with which PHY, clocks, resets, MMIO and IRQ? | board schematic/manual plus vendor DTS/driver | exact board integration |
| RQ2 | How are CPU DRAM addresses translated to DWMAC DMA addresses? | SoC TRM/IOMMU-System Port docs plus vendor driver | descriptor/buffer address contract |
| RQ3 | What cache-coherency, flush, and uncached-alias mechanisms exist? | SoC cache/System Port docs plus vendor cache code | legal CPU mapping and maintenance path |
| RQ4 | Which Synopsys DWMAC revision and descriptor/tail/status rules apply? | IP databook if public, otherwise pinned vendor stmmac sources | register/descriptor protocol, with authority caveat |
| RQ5 | What are the PLIC trigger, mask, clear, and rearm rules? | SoC interrupt docs plus DTS/vendor driver | interrupt lifecycle contract |

## Canonical Artifacts

| Artifact | Purpose | Hand-edited? |
|---|---|---|
| this file | scope, authority and claim boundary | yes |
| `megrez-network-hardware-source-ledger.md` | URL/revision/hash and extracted facts | yes |
| `riscv-dma-memory-type-contract.md` | Asterinas memory-type mapping | yes |
| `megrez-dwmac-rx-liveness-contract.md` | software model and sealed board evidence | yes |
| sealed `target/megrez-debug/...` JSON/logs | physical validation | no |

## Audited Claim Ledger

| Claim | Status | Authority | Caveat |
|---|---|---|---|
| EIC7700X does not implement Svpbmt | confirmed | TRM Part 1 MCPU feature summary; exact board DTB agrees | This is a SoC fact, not merely a boot-log inference. |
| EIC7700X exposes D0 DRAM through a non-coherent System Port alias | confirmed | TRM Part 1 Table 3-39 | The SoC window is 128 GiB; Asterinas intentionally accepts only the current 16-GiB board allocation subset. |
| Megrez has two independent RTL8211F RJ45 paths | confirmed | Megrez V1.1 schematic sheets 18 and 19; RockOS DTS | A link-bearing GMAC1 observation does not make GMAC1 the board's only wired port. |
| GMAC0/1 use MMIO `0x50400000`/`0x50410000` and summary IRQ 61/70 | confirmed | TRM Parts 1 and 4; pinned SoC DTS | Both summary interrupts are high-level PLIC sources. |
| The Ethernet IP reports Synopsys DWMAC 5.20 | confirmed | TRM Part 4 MAC Version register, reset `SNPSVER=0x52`; modern binding agrees | Full descriptor bitfields still rely partly on pinned vendor stmmac sources. |
| The shipped Megrez DT uses the older `win2030` binding contract | confirmed | pinned RockOS Megrez DTS and SoC DTS | Do not rewrite it to the newer upstream binding without a separate DT migration. |
| DWMAC receives identity DMA addresses in the shipped configuration | supported | pinned vendor DTS has no active GMAC IOMMU or `dma-ranges`; vendor stmmac uses the DMA API result | The public TRM does not by itself prove every interconnect translation detail. |
| Current ring tail/count/status handling matches this DWMAC integration | supported | TRM register descriptions plus pinned vendor stmmac behavior | TRM calls the tail the last valid descriptor; vendor ring code resolves the practical one-past/current-producer convention. |
| The observed TX-reclaim stall is caused by a stale cached CPU view | leading diagnosis | deterministic cache-line counterexample, no-Svpbmt fact, and sealed board observations | The legal uncached alias and mechanism are confirmed, but no post-alias board run has proved this was the physical root cause. |

## Open Risks

- Public manuals may be incomplete, mirrored without revision, or available
  only under NDA; absence must be recorded rather than filled by inference.
- EIC7700 cached/uncached addresses, DWMAC DMA windows, and Linux DMA API
  addresses may belong to different address domains.
- QEMU `virt` models VirtIO networking, not EIC7700 GMAC/cache/PLIC behavior;
  it cannot validate board-specific register semantics.
- A physical pass can validate one binary and configuration but cannot replace
  the missing hardware authority.

The pinned sources, page references, implementation comparison, and remaining
unknowns are recorded in `megrez-network-hardware-source-ledger.md`. No new
physical experiment is authorized by this research contract until a remaining
unknown is both material and cannot be separated by static or simulated tests.
