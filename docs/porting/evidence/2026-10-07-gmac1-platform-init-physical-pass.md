# GMAC-1 platform-init probe (2026-10-07)

## Scope

This run used the immutable menu candidate whose SHA-256 is
`fb46b354a238577d1e16c3674d0d835db8539065d5c128f0b72c149164665255` and the
kernel built from the current tree (`d6554801dcb9e902ee773fa3b2c781d149aa969ca4d8aead1e439d350e1f5832`).
The selected entry was exactly `gmac-1`, with the hardware watchdog armed.
No PHY, MDIO, DMA, IRQ, TX/RX, or network registration path was selected.

## QEMU gate

The rebuilt EIC7700 qtest suite passed 15/15.  The GMAC tests cover the
clock-gated fail-closed state, the six-register read-only contract, the
platform sequence, shared-reset isolation, and reset-gate behavior.  The
platform sequence leaves GMAC-0 readable and enables only the selected
GMAC-1 port before the six fixed reads.

## Physical result

The single watchdog-protected physical cycle is recorded in
`target/megrez-physical-step1/gmac1-platform-cycle-20261007-r1b/`.
It reached all six fixed offsets and passed:

```text
0x0000 -> 0x00000000
0x00b0 -> 0x00000009
0x0110 -> 0x00001052
0x0120 -> 0x21008145
0x1000 -> 0x00000000
0x1008 -> 0x00000000
```

The serial order was `mmio-map`, `platform-init`, `mmio-read`, six
`ATTEMPT`/`READ` pairs, `PASS`, and `DONE status=pass`.  No PHY/MDIO/DMA/
TX/RX/network marker occurred in the probe window.  The runner reported
`passed=true`, `probe_status=pass`, and `recovered=true` with elapsed time
37.83 s.

## Recovery

The board was returned to RockOS without changing persistent boot entries.
After reopening the management path, SSH reported kernel `6.6.87` and boot ID
`08dea784-79ae-40f2-a19b-f31caa08c0f7`; an authenticated sudo check returned
UID 0.  The recovery attestation is in
`target/megrez-physical-step1/gmac1-platform-rockos-attestation-20261007-r1b.log`.

This is a read-only register and recovery pass.  It does not qualify PHY
diagnostics, DMA, normal network registration, or packet traffic.  Because the
first corrected attempt passed and recovery was independently verified, no
additional physical retries were made.
