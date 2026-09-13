# Megrez SD High Speed evidence

Date: 2026-09-13

## Root cause

The desktop card reaches 4-bit, 208 MHz, 1.8 V SDR104 under RockOS, as reported
by `/sys/kernel/debug/mmc1/ios`. Before this change Asterinas always ended card
discovery at 4-bit, 25 MHz, 3.3 V and measured 9.79 MB/s for a direct 128 MiB
read. That result is 78 percent of the 12.5 MB/s wire ceiling and isolates the
remaining direct-I/O limit to SD timing negotiation rather than ext2 or the
existing SDMA bulk path.

## Implementation

- ACMD51 and CMD6 carry checked 8-byte and 64-byte data shapes.
- CSD command classes, SCR version, CMD6 High Speed support, and selected mode
  are parsed in the hardware-independent card layer.
- The host raises its SDHCI High Speed bit and clock to 50 MHz only after the
  card returns selected Function Group 1 mode 1.
- Unsupported and pre-switch failures remain at 25 MHz. An ambiguous
  post-switch failure performs one bounded baseline reinitialization.
- `asterinas.mmc_default_speed` skips promotion as a recovery escape hatch.
- One stable timing record identifies high-speed, forced, unsupported, or
  recovered operation without adding hot-path logs.

## Local verification

All commands used the existing persistent development container and
`CARGO_NET_OFFLINE=true`; no image or container was deleted.

The focused tests first failed for the intended missing behavior, then passed:

- metadata ACMD51/CMD6 command shape and SDMA rejection;
- CSD/SCR/CMD6 status parsing;
- successful High Speed negotiation;
- clean rejection and pre-switch transport fallback;
- ambiguous switch recovery exactly once and recovery failure propagation;
- SDHCI Host Control bit preservation;
- forced-default boot policy and default policy;
- strict physical evidence timing classification.

The complete component run used four QEMU harts:

```text
Platform HART Count         : 4
running 44 tests in crate "aster-mmc"
test result: ok. 44 passed; 0 failed; 0 filtered out.
[ktest runner] All crates tested.
```

The retained QEMU log is
`target/megrez-sd-high-speed-all-mmc-smp4-qemu.log`, SHA-256
`6f0d0b82356981c4aad92571bd7fa9ace7a70f3c95ab7d40ed8680a072428bbc`.

The evidence classifier passed all 12 Python unit tests. `cargo fmt --all --
--check`, `git diff --check`, and component-only warning-denying Clippy passed:

```text
cargo clippy -p aster-mmc --target riscv64imac-unknown-none-elf \
  --no-deps -- -D warnings
```

Clippy without `--no-deps` stops at the pre-existing
`ostd/src/io/io_mem/mod.rs:271` `needless_return` warning. The modified MMC
component itself is warning-free, so this unrelated workspace warning was not
changed.

The offline four-hart Sv39 release build passed and produced:

```text
target/osdk/aster-kernel-osdk-bin.Image
size: 5970664 bytes
sha256: 3e3c237caebc67d09d9d40730113a91dd222737800d96aca690a965af8887e44
```

## Physical acceptance

Physical timing, CRC, throughput, Firefox, and automatic RockOS recovery
results will be appended only after the recovery-armed experiments complete.
