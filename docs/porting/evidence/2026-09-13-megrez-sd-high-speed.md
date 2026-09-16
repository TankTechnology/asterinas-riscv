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

The release image was staged under a unique name and selected through a
temporary `sysboot` configuration. Partition 2 and both persistent selector
files were not rewritten. The tested image was:

```text
target/osdk/aster-kernel-osdk-bin.Image
size: 5970664 bytes
sha256: 3e3c237caebc67d09d9d40730113a91dd222737800d96aca690a965af8887e44
```

### Lightweight timing gate

The recovery-armed probe recorded the exact ordered sequence:

```text
[mmc] SDMA buffer cpu=0xfff00000 device=0xfff00000 bytes=524288
[mmc] controller 0x50460000 irq=81 sdma boundary=524288
[mmc] SDHC rca=43690 sectors=249737216 sector0=55aa
[mmc] timing=high-speed clock=50000000
[mmc] mmcblk0 registered read-only
```

The classifier passed and the board automatically returned to RockOS after a
fresh OpenSBI/U-Boot epoch. The retained result is
`target/firefox-current-performance/sd-hs-probe-2/result.json`; its serial log
SHA-256 is
`63ab9e991c9918579afd6bdf121aff66b474f3f48975a6830041dda7ab8f1db6`.

### Data correctness and throughput

A read-only RockOS mount established the immutable Firefox library baseline:

```text
/usr/lib/firefox-esr/libxul.so
bytes: 135186264
crc32: 90227f7f
```

Asterinas read the same file at 50 MHz and reproduced both byte count and
CRC32. The matched 128 MiB measurements were:

| path | 25 MHz baseline | 50 MHz result | change |
|---|---:|---:|---:|
| direct | 9.79 MB/s | 16.80 MB/s | +71.6% |
| buffered | 8.89 MB/s | 15.72 MB/s | +76.8% |

The direct path exceeds the required 35 percent improvement, all commands
returned zero, and the transcript contains no MMC failure, timeout, or
fallback record. The board performed a software reboot and returned to RockOS
in a fresh firmware epoch. The retained result is
`target/firefox-current-performance/sd-hs-io-5/result.json`; its serial log
SHA-256 is
`0be87ee36c9231446fe5dd2b10483d5a2d50ea097144e16da49bd10bd75d4e8d`.

### Firefox result

One desktop run reached the visible Firefox window after 122.71 seconds, but
its debug console and Firefox process started only after 97.31 and 95.62
seconds respectively. It is retained as an abnormal pre-Firefox system-start
sample and is not mixed into the matched cold-start mean. Once Firefox had
started, that run reached the window in 26.15 seconds, with an active service
and no restart.

The next two independently rebooted runs restored the normal approximately
seven-second console time and produced:

| run | console | visible Firefox window | Firefox process to window | service |
|---|---:|---:|---:|---|
| 2 | 7.34 s | 38.93 s | 32.87 s | active/running, `NRestarts=0` |
| 3 | 6.94 s | 39.47 s | 33.23 s | active/running, `NRestarts=0` |

The matched visible-window mean is 39.20 seconds, 9.4 percent below the prior
batched-page-cache mean of 43.27 seconds (40.33 and 46.21 seconds). The close
0.54-second spread also removes the prior 5.88-second spread. The retained
results are `sd-hs-firefox-2/result.json` and
`sd-hs-firefox-3/result.json` below `target/firefox-current-performance/`;
their serial SHA-256 values are respectively
`522e75617a268069de380439d4914e90d0f9f188d37dee41e8d0e391899c6f82`
and
`66639854745a960c06f7bbf06a5d32c2efcbdc1643e06f232309383c19ed9384`.

### Recovery and qualification lessons

- The EIC7700 hardware watchdog's maximum observed interval is approximately
  19 seconds. It is suitable for the lightweight probe, but it interrupts the
  longer CRC and throughput gate and must not be armed for that gate.
- A CRC over the first 32 MiB of the whole disk is not stable because RockOS
  mounts the boot partition read-write. A partition-2 CRC is also not stable
  because partition 2 is the Asterinas Debian root. The immutable `libxul.so`
  file is the correct cross-OS data-integrity object for this experiment.
- `loglevel=info` is used only until the one-time MMC mode evidence has been
  collected. The guest then lowers the console level before timing commands so
  unrelated warnings cannot corrupt machine-readable markers.
- After every experiment the board returned to RockOS. The final RockOS boot
  ID was `a7242560-500d-42ca-9273-523028736a50`, and the persistent selector
  hashes remained:

```text
02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26  /boot/extlinux/asterinas.conf
eb5f39a6e2db71ccc93ae005c488fd9f7ef353e75426e5a51e8923a9cbf2ebc5  /boot/extlinux/extlinux.conf
```
