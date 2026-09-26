# Megrez GPU CRG read-only probe

The device-tree resource gate has passed on the physical Megrez, but it does
not establish whether the GPU's three clocks are gated or its five reset
lines are deasserted. The pinned RockOS driver enables `aclk`, `cfg_clk`, and
`gray_clk`, sets the `aclk` rate, then pulses `axi`, `cfg`, `gray`, `jones`,
and `spu` reset controls before reading GPU registers. These operations use
the EIC7700 system CRG at `0x51828000`.

Add a separate `asterinas.gpu_crg_probe=1` boot flag. It first requires the
known Megrez GPU DT shape, exact GPU clock/reset IDs and names, and the CRG
aperture in the prepared DTB. Only then it reads the three clock control
words at offsets `0x12c`, `0x130`, `0x134` and the GPU reset word at `0x404`.
It prints one serial record with the raw words and decoded gate/deassert bits.
The read ranges are byte-disjoint from the existing MMC and DWMAC CRG
mappings. It must not write the CRG, touch the GPU aperture, load firmware,
or register a render device. A failed validation prints `status=skipped` and
does not map MMIO.

The gate will be checked in QEMU and one selected physical boot. Its output
establishes only the inherited clock/reset state at the time of the read;
firmware and active GPU rendering remain separate milestones. The selected
boot retains the root debug console, a recovery timer, RockOS default entry,
and a close/reopen root-control check.
