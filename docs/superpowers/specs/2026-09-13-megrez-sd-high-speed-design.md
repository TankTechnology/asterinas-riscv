# Megrez SD High Speed design

Date: 2026-09-13

## Goal

Raise the removable SD card used by the Megrez desktop root filesystem from
the SD default-speed mode (4-bit, 25 MHz, 3.3 V) to the standard SD High Speed
mode (4-bit, 50 MHz, 3.3 V). Keep the existing SDMA data path and preserve a
deterministic 25 MHz recovery path. Measure the resulting storage and Firefox
latency before considering UHS-I.

This is a protocol correction, not an unconditional clock increase. The card
must advertise and accept Function Group 1 High Speed through CMD6 before the
host changes its timing and clock.

## Decisive evidence

The current Asterinas card discovery ends with `ACMD6`, a 4-bit host-width
change, and a fixed 25 MHz clock. The driver neither reads the SCR nor issues
the data-bearing SD CMD6 switch-function command. Its direct 128 MiB read is
9.79 MB/s, which is 78 percent of the 12.5 MB/s payload ceiling of a 4-bit,
25 MHz bus.

On the same board and card, RockOS reports 4-bit SDR104 at an actual 208 MHz,
1.8 V and reads at approximately 91 MB/s. Existing Asterinas physical evidence
already establishes a 512 KiB SDMA buffer, the EIC7700 removable-card
controller, and a correct 32 MiB CRC read. The remaining direct-I/O ceiling is
therefore the negotiated SD timing mode, not ext2 page-cache granularity or the
absence of DMA.

Linux provides the reference sequence: read and decode SCR/CSD capabilities,
query CMD6 switch capabilities, issue CMD6 in switch mode, verify the selected
function in the returned 64-byte status, set host High Speed timing, and only
then raise the clock.

## Chosen scope

Implement only 3.3 V SD High Speed at 50 MHz in this milestone. Do not request
the OCR 1.8 V switch bit, issue CMD11, change regulator voltage, select an UHS
timing, or execute tuning. Those operations form a separate SDR104 milestone
because their failure and recovery contracts are materially different.

Keep the block device's read/write API and the 512 KiB SDMA transfer shape
unchanged. The new 8-byte SCR and 64-byte switch-status transfers use bounded
PIO during discovery; they are too small to justify extending the SDMA buffer
contract.

## Protocol and component design

### Data-command shape

Generalize the internal `Command` data description from an implicit 512-byte
block to an explicit checked block size and block count. Existing sector
constructors remain fixed at 512 bytes. Add constructors only for ACMD51
(`SEND_SCR`, one 8-byte read) and CMD6 (`SWITCH_FUNC`, one 64-byte read).

`MmioHost::command` programs the declared block size. `SdmaTransfer` continues
to reject anything other than 512-byte sector commands, so discovery metadata
cannot accidentally enter the bulk-data path.

### Capability discovery

After selecting the card, discovery will:

1. retain the CSD command-class bits while decoding capacity;
2. issue CMD55 plus ACMD51 and decode the SCR structure and SD specification;
3. require SD specification 1.10 or newer and the CSD switch command class;
4. issue CMD6 in check mode for Function Group 1;
5. inspect the returned support byte and continue only if High Speed is set.

Unsupported cards remain at 25 MHz without being treated as a probe failure.
Malformed SCR/CSD or internally inconsistent command data remains a hard
`Unsupported` error because it invalidates the card identity, not just the
optional timing mode.

### Timing transition

For a capable card, discovery issues CMD6 in switch mode with Function Group 1
set to High Speed and all other groups unchanged. It verifies that the returned
selected-function field is High Speed. Only after that verification does the
host set the SDHCI High Speed Enable bit and request 50 MHz.

Expose a small timing enum at the host boundary instead of Megrez-specific
boolean methods. The EIC7700 adapter supports `DefaultSpeed` and `HighSpeed`;
the card protocol remains independent of SDHCI register layout.

The resulting `Card` records its selected timing and data clock for one stable
boot-time diagnostic line. Per-request logging is forbidden on the I/O path.

## Failure and recovery contract

- A force-default-speed kernel flag skips SCR/CMD6 negotiation and retains the
  existing 25 MHz sequence. It is the board recovery escape hatch.
- A card that cleanly reports no High Speed support, or rejects the requested
  selected function while completing CMD6 correctly, remains usable at
  default speed.
- A transport, CRC, or timeout error during an optional capability check is
  logged once and falls back only while the card is known not to have changed
  mode.
- An error after the switch command may have changed card state. Discovery must
  clear host High Speed timing and restart the bounded baseline initialization
  once; it must not continue with an ambiguous host/card timing combination.
- If the baseline restart fails, MMC probe fails closed. There is no unbounded
  retry loop.
- All waits retain the existing polling budgets and data-line reset behavior.

The fallback result is observable as exactly one of `high-speed`,
`default-speed-unsupported`, `default-speed-forced`, or
`default-speed-recovered`. Logs include the selected clock, but no buffer
addresses beyond the existing SDMA boot record.

## Alternatives considered

### Raise the clock without CMD6

Rejected. A 50 or 208 MHz host clock does not itself change the card's timing
contract and can introduce data corruption or CRC failures.

### Implement SDR104 immediately

Deferred. It offers the largest eventual gain, but requires OCR S18R/S18A
negotiation, CMD11, a board regulator/PHY voltage transition, SDHCI UHS mode,
and tuning. Combining these with the first CMD6 implementation would make a
failed boot difficult to localize.

### Keep 25 MHz and optimize ext2 further

Rejected as the next storage step. Batched ext2 page-cache reads already
improved the 128 MiB buffered read from 3.88 to 8.89 MB/s and reduced the two
new Firefox-window measurements to a 43.27-second mean. Direct reads remain
near the 25 MHz wire ceiling, so filesystem work cannot remove that ceiling.

## Test strategy

Use test-first development and keep protocol logic hardware-independent:

1. pure command tests freeze ACMD51/CMD6 opcode, argument, response, direction,
   block size, count, and SDHCI transfer bits;
2. parsing tests cover CSD command classes, valid SCR versions, malformed SCR,
   CMD6 support bits, and selected-function fields;
3. fake-host discovery tests cover successful High Speed selection,
   unsupported-card fallback, rejected selection, forced default speed,
   pre-switch transport fallback, one bounded post-switch recovery, and failed
   recovery;
4. SDHCI tests freeze Host Control High Speed bit updates and prove non-sector
   commands cannot be submitted through SDMA;
5. RISC-V kernel tests run in QEMU, followed by the existing MMC component
   tests and an offline release build in the persistent development container.

This transition is single-threaded during device discovery and introduces no
shared mutable state or lock ordering. A concurrency model would not test its
actual risk. The bounded protocol state machine is instead covered by exact
fake-host traces, including the negative ambiguous-state transition and the
one-restart limit.

## Physical acceptance

Physical validation reuses the selectable boot menu and existing immutable
root filesystem. It never rewrites partition 2 or persistent U-Boot state.

1. Boot a light probe with automatic RockOS recovery armed.
2. Require the existing SDMA buffer/controller records and a new exact
   `high-speed clock=50000000` timing record.
3. Repeat the established 32 MiB read and CRC gate.
4. Run matched 128 MiB direct and buffered reads. Require direct throughput to
   improve by at least 35 percent over the 9.79 MB/s baseline without a new MMC
   error, timeout, or fallback record.
5. If storage gates pass, run two cold-profile desktop boots and retain Firefox
   service state, first-window latency, bounded dmesg, and recovery evidence.
6. Return to RockOS and verify its boot ID and the hashes of both persistent
   boot-selector files after every experiment.

Firefox latency is reported rather than given a hard pass threshold: storage
is only one part of startup, and this milestone must not disguise scheduler or
userspace costs as MMC failures.

## Completion boundary

This milestone is complete when High Speed is protocol-negotiated, all fallback
paths are regression-tested, the release image passes the physical CRC and I/O
gates, and two Firefox runs are published. It does not claim SDR104 parity with
RockOS or complete Firefox performance parity.
