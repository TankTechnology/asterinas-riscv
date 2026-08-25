# Megrez SDHCI M2b Write-Support Plan

> Execute this plan with TDD. Keep SD writes disabled by default. Do not write a real card until the live partition contract and rollback bytes have been verified.

**Goal:** Add bounded PIO writes for the Megrez SD card while allowing writes only through the recorded partition-2 node and only when explicitly armed by `asterinas.mmc_write_partition2`.

**Architecture:** Extend the existing SDHCI model with CMD24 and buffer-write-ready handling. Translate partition-relative BIO sectors with `SubmittedBio::sid_offset()`. Keep the whole disk and partitions 1/3 write-protected; permit partition 2 only when its exact start/length contract matches and the boot flag is present.

**Frozen partition-2 contract:** start LBA `0x000fa022`, sector count `0x00800000`, end-exclusive LBA `0x008fa022`.

---

## Task 1: Freeze partition translation and write policy

- Add pure ktests in `kernel/comps/mmc/src/block.rs` for physical LBA translation, overflow rejection, exact partition-2 identity, range bounds, and default read-only behavior.
- Run the focused RISC-V OSDK ktest compile and record the expected RED.
- Implement only the translation/policy helpers and make the focused compile GREEN.

## Task 2: Add CMD24 protocol support

- Add RED ktests for CMD24 encoding, little-endian 512-byte PIO writes, bounds checks, and data-line reset on failure.
- Extend `DataDirection`, `HostController`, and `Card` with bounded single-sector writes.
- Run focused OSDK ktest compile until GREEN.

## Task 3: Implement EIC7700 SDHCI write PIO

- Configure data registers for both read and write commands, but set the transfer-direction bit only for reads.
- Wait for Buffer Write Ready and write exactly 128 words to the data register.
- Preserve all existing bounded interrupt/error/reset behavior.

## Task 4: Integrate p2-only block writes

- Add the `aster-cmdline` dependency and define `asterinas.mmc_write_partition2`.
- Translate reads using `sid_offset`, fixing partition-node reads.
- Handle writes only when armed and only when `sid_offset` equals the frozen p2 start and the logical range fits its frozen length. Reject whole-disk, p1, p3, overflow, and malformed segments.
- Keep synchronous PIO flush semantics and log whether p2 writes are armed.

## Task 5: Local verification and candidate build

- Run scoped Rust formatting, strict Clippy for `aster-mmc`, and RISC-V OSDK ktest check for `aster-block`, `aster-mmc`, and `aster-kernel`.
- Build the RISC-V kernel in the pinned project container and record its SHA-256/size/U-Boot CRC32.
- Commit the implementation as one M2b milestone.

## Task 6: Real-board read-only regression

- Boot the candidate without `asterinas.mmc_write_partition2`.
- Verify SD discovery, partition registration, the known GPT hash, and explicit read-only logging.
- Stop at U-Boot after collecting serial evidence. Do not modify the SD card.

## Task 7: Prepare the reversible write gate

- Add a board-side gate that verifies the exact live p2 GPT contract before any write.
- The gate must back up the selected test sector, write a nonce pattern, read it back, restore the original sector, sync, and verify the restored hash.
- Test the gate logic locally with mocks. Do not execute the destructive portion on the real board until the exact sector and recovery evidence are shown immediately before the operation.
