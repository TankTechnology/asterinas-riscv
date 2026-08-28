# Megrez Network Hardware Source Ledger

Date: 2026-08-28

This ledger binds the Megrez Ethernet implementation to versioned hardware
authority. It separates hardware facts from vendor software conventions and
from physical observations. Local cached PDFs and repositories are working
copies only; their hashes and upstream identities, not their cache paths, are
the durable provenance.

## Source identities

| Level | Source | Revision and identity | Scope |
|---|---|---|---|
| A | [EIC7700X TRM release](https://github.com/eswincomputing/EIC7700X-SoC-Technical-Reference-Manual/releases/tag/v1.0.0-20250103) | tag `v1.0.0-20250103`, tag commit `2ec9a3ac6283caef37e68ed9a0d5983bcbfb35a9`; Part 1 SHA-256 `f1d7adef279fae2c83cca7c27e31226180bdfbf01c42384c01bf6d369e195361`; Part 2 `955c99f00934e5bc342e4b34b5dc3d73179d66a8395ae860877b21a6a9b8def5`; Part 3 `50402053a54ba05552b31efbc04c49fadbe154e50477730d4a4a462836095387`; Part 4 `8cd63b510a8800f9a344979edc71511367e3033bbeee1516b7ee5fd33a49d250` | CPU ISA, address map, cache maintenance, interrupts, Ethernet registers |
| A | [Milk-V Megrez resource page](https://milkv.io/docs/megrez/getting-started/resources), Megrez V1.1 schematic | PDF size 1,841,180 bytes; SHA-256 `aa3b0f59842619d966c64139aca47d5afd0e011b7c5d4aca15f6262ad87e3c68` | RJ45, RTL8211F, RGMII, MDIO and reset wiring |
| A | same official resource page, Megrez V1.1 component placement | top PDF size 314,825 bytes, SHA-256 `7c7087437968fc188179089e76323c1a1163160625486a27953c8ac2242db160`; bottom PDF size 254,288 bytes, SHA-256 `400c61813aa914a999f824bd799a78229072e2b57eee0497455d8c69e21f80a8` | board-side component and connector location |
| A | [upstream EIC7700 Ethernet binding](https://android.googlesource.com/kernel/common/+/6a08076f009e3d9460bebae9f209c1dc1d8a46b7/Documentation/devicetree/bindings/net/eswin,eic7700-eth.yaml) | Linux common commit `6a08076f009e3d9460bebae9f209c1dc1d8a46b7`; SHA-256 `95f9ea66029dd9b2947ffcdf4f083f37434d18a5151768b4b82cac029190fc9f` | modern `eic7700-qos-eth` DT ABI and DWMAC 5.20 compatibility |
| B | [ESWIN Linux](https://github.com/eswincomputing/linux-stable/tree/fc6038c00e006226e3bd504d2679c534eabf5503) | branch `linux-6.6.18-EIC7X`, commit `fc6038c00e006226e3bd504d2679c534eabf5503` | vendor SoC DTS, GMAC glue, DMA/cache implementation |
| B | [RockOS Megrez Linux](https://github.com/rockos-riscv/rockos-kernel/tree/bf2ec5d53002c16bc1bc593b92516eb6c2866176) | branch `rockos-v6.6.y`, commit `bf2ec5d53002c16bc1bc593b92516eb6c2866176` | board DTS and shipped Megrez GMAC tuning |
| C | Asterinas source at this branch | repository commit recorded with each implementation change | implementation under audit, not independent hardware authority |
| D | sealed serial and runner evidence | hashes in `megrez-dwmac-rx-liveness-contract.md` | one observed configuration only |

The TRM GitHub release was published on 2025-01-03, while its four current
assets report update timestamps on 2026-01-21. The tag, asset IDs, sizes, and
hashes are therefore retained together; the tag name alone is not treated as
a byte identity.

| TRM asset | GitHub asset ID | Size (bytes) | Asset update time |
|---|---:|---:|---|
| Part 1 | 343592641 | 22,772,021 | 2026-01-21T03:13:06Z |
| Part 2 | 343603475 | 14,641,974 | 2026-01-21T03:44:41Z |
| Part 3 | 343621954 | 17,621,503 | 2026-01-21T04:53:37Z |
| Part 4 | 343645181 | 11,297,029 | 2026-01-21T06:00:16Z |

The exact vendor files used for comparison are:

| Repository file | SHA-256 |
|---|---|
| ESWIN `arch/riscv/boot/dts/eswin/eswin-win2030-die0-soc.dtsi` | `fcafbd86ea71d78886702081e77124973f43e7bb8073e64a8db4cd7785f3d35f` |
| ESWIN `drivers/net/ethernet/stmicro/stmmac/dwmac-win2030.c` | `6abf28f712ddcd9a4d9eb7a108033c76e8079de9dc79c3529575c2968d835f34` |
| ESWIN `arch/riscv/mm/dma-noncoherent.c` | `dccb4c36461650f59cd18ef768358c395d86c7c157c0c488790dede1506c1fd6` |
| ESWIN `arch/riscv/include/asm/pgtable-64.h` | `1140390b0a3657c1b9e0eda0dc3f256c982b25b2f09e46ada28be17e8f7c574c` |
| ESWIN `drivers/net/ethernet/stmicro/stmmac/stmmac_main.c` | `bed987608cdb21b1c48dfdfd454b11781cf6ce2ec9a3a47e266f1a13c64b127e` |
| ESWIN `drivers/net/ethernet/stmicro/stmmac/dwmac4_dma.c` | `60e2b0bc9dd46e1df80fb886c47a02a0915611d4ec634435cd4cc219d76e0b0c` |
| ESWIN `drivers/net/ethernet/stmicro/stmmac/dwmac4_descs.c` | `557b8e9defa482166a2eeb2767a7ae5dc405d978cc2a080e036b2a3855dd0f3e` |
| ESWIN `drivers/net/ethernet/stmicro/stmmac/dwmac4_lib.c` | `2543cf88e08e3798f31810b8104c71a1d6d8730776c6a07c8dc4860835f0e72b` |
| RockOS `arch/riscv/boot/dts/eswin/eic7700-milkv-megrez.dts` | `8d96c16ce74cb3d5fceedd9301cab8d7466170f7ed68ce587f36e3dd8dff4ef5` |
| RockOS `arch/riscv/boot/dts/eswin/eswin-win2030-die0-soc.dtsi` | `05f8bdc7f70503050ce137ac853cce1bb1c1638da50dc5c0d2632ef3640ddff3` |
| RockOS `drivers/net/ethernet/stmicro/stmmac/dwmac-win2030.c` | `20f0e6b7d09a2caf58951c62c3078ac5523a2d1a15ed93be6100587a2506277e` |

## RQ1: board integration

Megrez V1.1 schematic sheets 18 and 19 show two separate Ethernet paths:

- `RJ45_1` connects through an RTL8211F-CG to `RGMII0`, with its own
  `RGMII0_MDC`, `RGMII0_MDIO`, reset, interrupt and strap network.
- `RJ45_2` connects through another RTL8211F-CG to `RGMII1`, with its own
  `RGMII1_MDC`, `RGMII1_MDIO`, reset, interrupt and strap network.

The pinned RockOS Megrez DTS enables both `d0_gmac0` and `d0_gmac1`, assigns
aliases `ethernet0` and `ethernet1`, and supplies separate reset GPIOs and RGMII
delay values. The delay values in `tools/riscv/megrez_gmac_contract.v1.json`
and `kernel/comps/dwmac/src/arch/riscv.rs` exactly match that board DTS.

The SoC TRM Part 4 gives GMAC0/1 bases `0x50400000` and `0x50410000` and HSP
base `0x50440000`. TRM Part 1's interrupt table assigns summary interrupts
`eth0_sbd_intr`=61 and `eth1_sbd_intr`=70, both high level. These values match
the pinned SoC DTS and Asterinas.

Conclusion: both ports are real board paths. The prior GMAC1 link observation
only identifies which jack/cable path was active in that run.

## RQ2: DMA address domain

The pinned SoC DTS marks the platform and each GMAC `dma-noncoherent`. Its GMAC
IOMMU entries are commented out and it declares no GMAC `dma-ranges`. The
vendor stmmac driver programs addresses returned by Linux's DMA API. This is
evidence that the shipped configuration uses the resulting physical DMA
address directly; it is not a general proof that every EIC7700 configuration
has an identity DMA map.

Asterinas preserves the allocation's original physical and device address when
it creates the CPU-only uncached alias. That is consistent with this pinned DT
contract. Any future DT enabling an IOMMU or a DMA translation window must be
treated as a different contract and rejected until implemented.

## RQ3: cache and non-coherent aliases

TRM Part 1 supplies the missing primary authority:

- PDF page 186, Table 3-4 lists `RV64GC_Zba_Zbb_Sscofpmf`; Svpbmt is absent.
- PDF pages 218-220, Tables 3-37 and 3-39 distinguish cacheable Memory Port
  accesses from the System Port and map D0 DRAM at
  `0xC0_0000_0000..0xDF_FFFF_FFFF` as 128 GiB of non-coherent memory.
- PDF page 295 identifies L3 `Flush64` at controller offset `0x0200`; PDF pages
  299-301 define write-back/invalidate propagation and the required fences.

The L3 controller base is `0x0201_0000`, so Asterinas register
`0x0201_0200` is the documented base plus `Flush64`. Its range flush iterates
64-byte lines and issues an I/O fence after every register write. This uses the
documented mechanism; the ordering itself remains separately covered by the
Asterinas memory-ordering contract and tests.

The current Asterinas alias range `0xC0_0000_0000..0xC4_0000_0000` is not the
whole SoC alias. It is a checked 16-GiB subset corresponding to the current
board allocation range `0x8000_0000..0x4_8000_0000`. Requests outside that
subset fail closed.

Pinned vendor Linux independently follows the same split: streaming DMA uses
cache write-back/invalidate operations, while `arch_dma_set_uncached` converts
D0 Memory Port addresses to the D0 System Port and maps that view uncached.
This validates the architecture of uncached descriptor rings plus explicitly
synchronized packet buffers.

## RQ4: DWMAC register and descriptor protocol

TRM Part 4 identifies two Synopsys Ethernet modules. The MAC Version register
at offset `0x110` resets with `SNPSVER=0x52`, i.e. DWMAC 5.20. It documents:

- TX tail at `0x1120`, RX tail at `0x1128`, and ring length at `0x112c`;
- ring length as descriptor count minus one, with 1024 descriptors encoded as
  `0x3ff`;
- channel status as write-one-to-clear;
- receive interrupt `RI` at bit 6;
- transmit-buffer-unavailable `TBU` at bit 2, with resume after ownership and
  tail/poll-demand update.

Asterinas register offsets, ring-length encoding, status clearing and interrupt
bits match these definitions. The TRM's prose describes a tail as the last
valid descriptor, while pinned vendor stmmac ring code uses the effective
current-producer/one-past convention also used by Asterinas. Because the public
TRM does not expose every descriptor bitfield, those remaining details stay at
Level B rather than being promoted to hardware facts.

TRM Part 4 PDF pages 202-203 document
`MTL_RxQ0_Missed_Packet_Overflow_Cnt` at offset `0xd34`. Bits 26:16 count
packets discarded by DMA buffer unavailability, bits 10:0 count packets
discarded by receive-queue overflow, and bits 27 and 11 report counter
overflow. All four fields clear when the register is read. Asterinas decodes
and cumulatively records this register only at its existing bounded progress
milestones. It does not enable a counter block or change queue state. This
primary-source counter is the next discriminator between loss before the DMA
ring and loss above the MAC after the post-fix physical run observed neither
DMA RBU nor descriptor errors.

## RQ5: PLIC lifecycle

The summary IRQs are documented as high-level sources. Asterinas masks the
source before deferring work, fences, completes the claim, drains channel
status/work, and only then rearms the source. That ordering is appropriate for
a level-triggered source: rearming before draining would allow the still-high
line to retrigger immediately. No edge-trigger assumption is permitted for
IRQs 61 or 70.

## Device-tree version boundary

The modern upstream binding uses compatible strings
`"eswin,eic7700-qos-eth", "snps,dwmac-5.20"`, clock names
`axi/cfg/stmmaceth/tx`, reset name `stmmaceth`, and scalar RX/TX internal-delay
properties. The shipped RockOS Megrez tree instead uses the older vendor ABI:
`"eswin,win2030-qos-eth"`, reset name `ethrst`, three vendor clocks encoded as
six DT cells, HSP register tuples, and three-word per-speed delay arrays.

Asterinas intentionally validates the exact shipped old ABI today. The modern
binding is evidence of the IP revision, not permission to reinterpret an old
DTB. Supporting both requires two explicit parsers/contracts and tests for each
version; silently combining properties is forbidden.

## Implementation audit

| Boundary | Authority | Asterinas status | Result |
|---|---|---|---|
| two RTL8211F/RJ45 paths | schematic + RockOS DTS | two exact GMAC candidates | aligned |
| MMIO/HSP/summary IRQ | TRM + SoC DTS | exact ranges and IRQ 61/70 | aligned |
| clocks/reset/RGMII delays | RockOS DTS + vendor glue | old-ABI exact validation and programming | aligned for pinned DT |
| DWMAC revision | TRM MAC Version | accepts GMAC4/5, observes 5.20 | aligned |
| ring count/status/interrupts | TRM Part 4 | count-minus-one and W1C masks | aligned |
| DMA address | pinned DT/vendor DMA API | retains original device address | supported for pinned DT |
| descriptor CPU mapping | TRM System Port + vendor Linux | checked uncached alias | aligned; post-fix hardware result pending |
| packet-buffer synchronization | TRM cache flush + vendor Linux | streaming DMA uses Zicbom, PBMT_NC, or checked System Port alias | statically aligned; post-fix hardware result pending |
| PLIC lifecycle | TRM high-level IRQ | mask, complete, drain, rearm | aligned |

## Remaining unknowns and next evidence

No additional board probing is justified merely to rediscover a documented
constant. Before another physical run, static and simulated checks should:

1. prove every programmed ring/buffer address came from the pinned DMA address
   path and that an enabled IOMMU/translation is rejected;
2. preserve the exact 5.20 descriptor/status/tail model in host tests;
3. prove allocations outside the supported 16-GiB alias subset fail before
   any descriptor or packet buffer is exposed to hardware;
4. compile the packet-buffer bounce path so that the no-Zicbom/no-Svpbmt case
   must consume the checked System Port alias.

A device-specific public RTL8211F datasheet was not located on Realtek's
official site during this audit. PHY identity and board straps are therefore
bound to the Milk-V schematic and pinned Linux behavior rather than an
unversioned third-party PDF. This limits claims about undocumented PHY quirks;
it does not weaken the schematic fact that each RGMII path has its own PHY and
MDIO bus.

None of these items needs a board. After they pass, one recovery-armed physical
run should validate the already selected packet-buffer fix rather than start a
new open-ended diagnosis.
