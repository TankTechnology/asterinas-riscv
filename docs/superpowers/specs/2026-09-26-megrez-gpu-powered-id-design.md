# Megrez GPU powered identity gate

The physical CRG snapshot from `9ca5f7f2d` showed `aclk=0x20`, `cfg=0`,
`gray=0`, and `reset=0`: all three GPU gates off and all five reset lines
asserted. Reading the GPU aperture in that state has no established safety
contract. The RockOS source at `bf2ec5d5` instead enables three clocks and
pulses `axi`, `cfg`, `gray`, `jones`, and `spu` resets before it reads GPU
registers. Its clock driver defines a 1.6 GHz parent and the current `aclk`
divider field `2` as divide-by-two, so this selected board state already
selects 800 MHz. The Volcanic driver reads a 64-bit `RGX_CR_CORE_ID` at GPU
offset `0x20`, with B, V, N, C in four 16-bit fields.

Add an independently gated `asterinas.gpu_powered_id_probe=1` stage. It first
checks the same exact DT/CRG contract and requires the observed initial CRG
tuple. It turns on only the three GPU gate bits, verifies readback after each
write, delays at least 15 microseconds per reset line, deasserts the five
GPU reset bits in RockOS order, and checks final CRG readback. Only then may
it read the 64-bit GPU ID at `0x51400020`; the accepted raw value encodes
`30.3.408.101`. Whether the ID matches or not, it must reassert the five
resets and restore the original clock words, then verify the restored state.
If an operation fails, emit a bounded serial failure marker and leave the
software recovery timer armed. It does not load firmware, register a render
node, or enable Firefox GPU compositing.

The implementation stays in safe Rust with `IoMem`, uses small exclusive
CRG ranges disjoint from MMC and DWMAC, and has a mock-register kernel test
for write order, readback checks, and restoration on ID mismatch. The
physical selected boot retains the default RockOS entry, the root debug
console, and the software recovery timer until serial/root/desktop checks
pass. The person at the board can reset if the GPU MMIO read stalls before
the timer can run.
