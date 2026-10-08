# Megrez DWMAC Document-Driven Preboard Design

Date: 2026-08-28

## Goal

Turn the Megrez wired-network assumptions that are now supported by the EIC7700
TRM, the Megrez schematic, and the shipped vendor device tree into executable
contracts before another physical run. The result must reject a translated DMA
domain that the current driver does not implement, model the documented DWMAC
5.20 descriptor/tail/status rules, prove the supported EIC7700 uncached-alias
bounds, and emit enough address evidence to classify the next board run.

This milestone does not try to make networking pass by changing queue policy or
cache operations. It narrows and observes the contract first.

## Authority boundary

The committed hardware source ledger is the authority index for this design.
The relevant facts are:

- EIC7700 integrates Synopsys DWMAC 5.20 at `0x50400000` and `0x50410000`;
- the Megrez schematic connects both controllers to RTL8211F/RJ45 paths;
- the shipped vendor DT marks the ports non-coherent and does not activate an
  IOMMU or a `dma-ranges` translation for either GMAC;
- the board CPU does not advertise Svpbmt;
- EIC7700 exposes a Die 0 uncached alias, while Asterinas intentionally supports
  only the checked 16 GiB subset `0xc0_0000_0000..0xc4_0000_0000`.

The absence of IOMMU/`dma-ranges` is a property of the frozen shipped
configuration, not a universal EIC7700 property. A future translated DT must be
rejected until the driver deliberately implements that address domain.

## Alternatives

### A. Run the board first and add logs around the observed failure

Rejected. This gives quick observations but repeats the slow, reset-prone loop
that the hardware research was intended to replace. It also cannot tell whether
an address-domain mismatch was already present before the run.

### B. Express every check only as kernel ktests

Rejected as the sole strategy. Kernel tests are close to production, but their
compile/run cycle is too expensive for the descriptor protocol's small state
space and does not make counterexamples easy to inspect.

### C. Hybrid reference model plus narrow production tests (chosen)

Use the existing host Rust model for exhaustive DWMAC 5.20 protocol checks;
bind it to focused source contracts and kernel ktests for the real parser and
DMA abstraction. Run one RISC-V compile gate after focused host tests. Do not
touch the board in this milestone.

## Executable contracts

### Frozen DT DMA domain

Both the host DT inspector and the in-kernel exact Megrez parser must require:

- `dma-noncoherent` is present;
- `iommus` is absent;
- `dma-ranges` is absent.

The checks occur before MMIO programming or DMA allocation. Presence of either
translation property is a stable contract error, not a fallback to identity
DMA.

### DWMAC 5.20 controller and queue protocol

The exact Megrez platform accepts `MAC_VERSION.SNPSVER == 0x52` for both
controllers. Other revisions fail closed because this implementation is bound
to the documented EIC7700 instance rather than to all DWMAC4/5 variants.

The host model freezes these normal-descriptor rules:

- descriptors are 16 bytes and carry the 64-bit buffer address in words 0/1;
- CPU publishes descriptor body before OWN and publishes the tail after OWN;
- receive descriptors use OWN, IOC, and BUF1V;
- one-buffer transmit descriptors use OWN, FD, LD, and the exact length;
- a 64-entry ring is programmed as `63`;
- initial TX tail equals TX ring base;
- initial RX tail is one-past the 64-entry RX ring;
- later TX/RX tails name the next descriptor modulo the ring;
- channel status acknowledgement writes only the known write-one-to-clear bits,
  while RBU triggers a receive-tail resume.

The model is deliberately smaller than the driver. It is an independently
readable oracle for the documented state transitions, not a second driver.

### EIC7700 CPU/DMA address evidence

`DmaCoherent` continues to expose the original backing physical address and the
device DMA address. A read-only diagnostic accessor additionally exposes the
uncached alias physical address when the platform-alias path is active. It does
not expose ownership or allow mutation.

The DWMAC queue snapshot carries:

- descriptor backing physical address;
- descriptor device/DMA address;
- optional uncached-alias physical address;
- TX/RX ring device addresses and their initial tails.

Driver initialization emits one bounded marker containing the selected MAC
revision and those addresses. On Megrez, the expected relationship is identity
`ring_paddr == ring_daddr` plus a checked EIC7700 CPU alias. A missing alias or
translated DMA address is evidence, not something the log tries to repair.

## Safety and failure behavior

- No new `unsafe` is added to `kernel/`; the only new OSTD API is read-only.
- All DT translation rejection happens before device mutation.
- Address arithmetic remains checked by the existing alias-range helper.
- The descriptor model cannot write MMIO or allocate memory.
- Logs are emitted once during initialization, not per packet.
- This milestone performs no QEMU networking run and no physical-board run.

## Validation

1. A host test first proves the current DT inspector accepts synthetic
   `iommus`/`dma-ranges`, then turns green after rejection is implemented.
2. Kernel ktests freeze the same `PortFields` absence contract and exact 0x52
   controller revision.
3. The host Rust model exhaustively checks the documented descriptor, tail, and
   status transitions; Python compiles and runs it with `-Dwarnings`.
4. OSTD ktests cover the complete supported alias window and rejection exactly
   outside it, plus diagnostic preservation through `Split`.
5. Source-contract tests require the one-shot diagnostic marker and all three
   address domains.
6. Run the focused host targets, formatting/static checks, then one pinned
   RISC-V `cargo osdk check --ktests`. No repeated heavy gate is required.

## Non-goals

- implementing an IOMMU or arbitrary `dma-ranges` translation;
- changing packet-buffer allocation, TCP, IRQ policy, or PHY tuning;
- claiming that stale CPU descriptor state is the proven physical root cause;
- downloading an unofficial RTL8211F datasheet;
- booting Linux instead of Asterinas;
- starting another board experiment before this contract is green.
