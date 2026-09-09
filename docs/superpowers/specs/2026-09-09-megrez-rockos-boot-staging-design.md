# Megrez RockOS Boot-Staging Fast Path Design

## Goal

Replace repeated serial YMODEM transfer of Megrez boot artifacts with one
bounded RockOS network-staging transaction.  After staging, U-Boot loads the
same frozen bytes from eMMC partition 1 and Asterinas remains the only kernel
allowed to write or validate the Debian target on partition 2.

The immediate success criterion is that repeated installer and graphical boots
do not send the kernel or initramfs over YMODEM.  Every staged file must remain
bound to the current debug plan and must pass host SHA-256, RockOS SHA-256, and
U-Boot CRC32 checks before `booti`.

## Boundaries

RockOS is a maintenance transport, not part of the Asterinas acceptance path.
It may:

- receive immutable boot files over the private board/host network;
- write new, uniquely named files below the existing partition-1 `/boot`;
- calculate hashes, call `sync`, and reboot normally to U-Boot.

RockOS must not mount, modify, hash for acceptance, or otherwise validate
partition 2.  The workflow must not edit the persistent U-Boot environment,
replace an extlinux default, overwrite an existing boot artifact, or use a
RockOS userspace result as evidence for Firefox or Asterinas correctness.

The existing YMODEM path remains an explicit recovery fallback.  It is no
longer the default for a boot whose plan-bound files have been staged and
verified.

## Selected Approach

Add a small host-side RockOS staging command and teach the existing install and
physical-graphics launchers to consume its immutable result.  This reuses the
already implemented `BoardSession` MMC loader instead of adding another U-Boot
transport.

The alternatives are intentionally deferred:

- staging the compressed Debian root chunks on partition 1 could remove the
  Asterinas HTTP transfer, but first requires a measured partition-1 capacity
  and a new read-only source-partition contract;
- allowing RockOS to write partition 2 would be faster but would stop testing
  the Asterinas MMC write path, so it is rejected.

## Staging Bundle

The host prepares only the bytes U-Boot must load:

- the plan's raw Asterinas kernel Image;
- the generated installer or graphical Stage1 initramfs;
- a patched Megrez DTB only when the DTB is not already available under its
  frozen partition-1 path;
- a canonical manifest containing the plan SHA-256, Git commit, file name,
  byte size, SHA-256, CRC32, and U-Boot load address for every file.

Each destination basename contains the short Git commit and a prefix of the
plan SHA-256.  Staging never reuses a mutable generic name.  Re-running an
identical bundle is idempotent: a matching destination is accepted after
rehashing, while a mismatching existing destination is a hard failure and is
never overwritten.

## Transaction

1. Revalidate the clean Git identity, preboard permit, debug plan, and every
   source artifact before opening the serial device.
2. Start a bounded HTTP server on the existing private host interface.  Serve
   only the pinned bundle directory; do not provide DHCP or modify host network
   configuration.
3. From a fresh U-Boot prompt, run the explicit RockOS entry:
   `sysboot mmc 1:1 any 0x88200000 /extlinux/extlinux.conf`, then select
   `RockOS GNU/Linux 6.6.87-win2030`.
4. Use the RockOS maintenance console to check free `/boot` space.  Download
   each file to `/tmp/<name>.part`, verify its size and SHA-256, and install it
   under the unique partition-1 name.  Credentials are supplied through an
   interactive or already-open descriptor and are never placed in argv,
   environment variables, repository files, result JSON, or the serial log.
5. Call `sync`, hash each installed `/boot` file again, and publish a staging
   result only when every value matches the canonical manifest.
6. Reboot RockOS normally.  Require a new OpenSBI/U-Boot epoch and a fresh
   prompt; never use a physical reset as the success path.
7. Load the staged files with `ext4load mmc 1:1`, validate U-Boot's reported
   size and CRC32, patch only the current in-memory FDT properties required by
   the plan, and perform the single bounded `booti`.

The staging result is content-bound and reusable by later boots of the same
plan.  Launchers reject it if the plan, commit, filename, size, SHA-256, CRC32,
or recovery epoch differs.

## Failure and Recovery

Every external phase has a deadline.  A download or hash failure leaves only a
`.part` file in `/tmp`; publication to `/boot` occurs only after verification.
If space is insufficient, a filename already exists with different bytes,
RockOS cannot reboot, or U-Boot reports a different size/CRC, the command
publishes a failed diagnostic result and does not run `booti`.

Cleanup may remove only this transaction's `/tmp/*.part` files.  It must not
delete old `/boot` artifacts automatically.  A failed or unavailable RockOS
path returns the board to a U-Boot prompt when possible and reports that the
operator may choose the existing YMODEM fallback.

## Interfaces and Evidence

The workflow has three explicit CLI phases rather than one command with hidden
fallbacks:

1. A consumer-specific `prepare-staging` phase builds its final initramfs and
   writes a canonical boot-staging manifest below `target/`.  Preparation does
   not open the serial device.
2. A generic `rockos-stage` phase accepts only that manifest, the serial-by-id
   device, the host interface, and an output directory.  It performs the
   RockOS transaction and publishes the content-bound staging result.
3. The existing `install` or physical-graphics launch accepts
   `--staging-result` and uses the manifest's partition-1 basenames with the
   existing MMC loader.  Without a staging result, the caller must explicitly
   select `--load-transport ymodem` for recovery; supplying an invalid result
   fails before any board command.

The exact subcommand spelling may follow the owning CLI's existing parser
layout, but these three state transitions and their separate evidence files
are required.  Preparation and staging outputs use descriptor-relative atomic
publication under the repository's `target/` tree.

The new staging result records:

- schema version, pass/fail reason, plan SHA-256, and Git commit;
- the complete immutable manifest identity;
- the RockOS kernel-release observation;
- pre-install and post-`sync` hashes for every destination;
- the new firmware epoch and fresh U-Boot prompt;
- relative evidence filenames for the bounded serial transcript and manifest.

The installer and physical-graphics commands gain an explicit staged-MMC input.
When present, it is required to match the current plan and the generated
initramfs exactly; there is no silent fallback to YMODEM after a staging-result
validation failure.  YMODEM is selected only through its existing explicit
mode for recovery work.

## Verification

Host tests cover canonical manifest parsing, duplicate and traversal rejection,
unique filenames, idempotent matching destinations, refusal to overwrite,
credential redaction, timeout/error cleanup, plan/result drift, and exact MMC
launcher arguments.  Existing YMODEM tests remain unchanged as fallback
coverage.

A small QEMU/fake-console integration test exercises the full state machine
without board writes.  The physical proof then stages one current kernel and
initramfs through RockOS, observes a fresh U-Boot epoch, loads both through MMC,
checks their CRC32 values, and reaches the next Asterinas milestone.  Firefox
acceptance still requires the separate real HDMI, keyboard, mouse, serial, and
recovery evidence; a successful RockOS staging result alone makes no graphical
claim.

## Expected Performance

The measured YMODEM path transferred a 3.1 MiB compressed kernel plus an
11.5 MiB compressed installer at roughly 50 KiB/s, costing about four to five
minutes per attempt.  Network staging pays that cost once and subsequent
`ext4load` operations use local eMMC.  The optimization does not remove the
intentional Asterinas partition-2 per-chunk readback or final 2 GiB SHA-256,
because those checks are part of the kernel validation rather than transport
overhead.
