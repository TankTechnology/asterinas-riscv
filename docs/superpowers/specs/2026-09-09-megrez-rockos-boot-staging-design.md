# Megrez RockOS Boot-Staging Fast Path Design

## Goal

Replace repeated serial YMODEM transfer of Megrez boot artifacts with one
bounded RockOS network-staging transaction.  After staging, U-Boot loads the
same frozen bytes from eMMC partition 1 and Asterinas boots the already
installed Debian target on partition 2.

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

The current 2 GiB partition-2 root has already passed its complete installation
and readback SHA-256 gate.  Routine Firefox boots reuse it without any
partition-2 provisioning step.  Rebuilding or reinstalling that root is outside
this fast-path milestone and occurs only after an intentional rootfs change.

The existing YMODEM path remains an explicit recovery fallback.  It is no
longer the default for a boot whose plan-bound files have been staged and
verified.

## Selected Approach

Use the documented RockOS maintenance procedure, the existing bounded host HTTP
server pattern, and the already implemented `BoardSession` MMC loader.  Do not
add a generic staging framework.  The only code change allowed by this design
is the smallest adapter needed for the current physical-graphics launcher to
accept verified partition-1 basenames and select its existing MMC transport.

The alternatives are intentionally excluded:

- staging or reinstalling the Debian root is unnecessary while its frozen
  identity is unchanged;
- adding a new RockOS provisioning service or generic transfer protocol would
  delay the Firefox experiment without improving its evidence.

## Staging Bundle

The host stages only the bytes U-Boot must load for the next graphical boot:

- the plan's raw Asterinas kernel Image;
- the existing graphical Stage1 initramfs;
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
4. Use the existing RockOS maintenance console to check free `/boot` space.
   Download each file to `/tmp/<name>.part`, verify its size and SHA-256, and
   install it under the unique partition-1 name.  Credentials are supplied
   through an interactive or already-open descriptor and are never placed in
   argv, environment variables, repository files, result JSON, or the serial
   log.
5. Call `sync`, hash each installed `/boot` file again, and retain the command
   transcript plus canonical manifest as the staging evidence.
6. Reboot RockOS normally.  Require a new OpenSBI/U-Boot epoch and a fresh
   prompt; never use a physical reset as the success path.
7. Load the staged files with `ext4load mmc 1:1`, validate U-Boot's reported
   size and CRC32, patch only the current in-memory FDT properties required by
   the plan, and perform the single bounded `booti`.

The recorded staging evidence is reusable by later boots of the same plan.
Before each boot, the existing U-Boot loader still rejects a different
filename, size, or CRC32.

## Failure and Recovery

Every external phase has a deadline.  A download or hash failure leaves only a
`.part` file in `/tmp`; publication to `/boot` occurs only after verification.
If space is insufficient, a filename already exists with different bytes,
RockOS cannot reboot, or U-Boot reports a different size/CRC, the procedure
stops and does not run `booti`.

Cleanup may remove only this transaction's `/tmp/*.part` files.  It must not
delete old `/boot` artifacts automatically.  A failed or unavailable RockOS
path returns the board to a U-Boot prompt when possible and reports that the
operator may choose the existing YMODEM fallback.

## Interfaces and Evidence

Use the existing commands instead of introducing a new public CLI:

1. serve the plan-bound staging directory with the existing bounded HTTP-server
   helper, bound only to the private host interface;
2. run the documented RockOS `sysboot` entry and shell commands through the
   existing serial console;
3. invoke `tools.riscv.megrez_board_session` with
   `--load-transport mmc`, the unique partition-1 basenames, and the plan's
   existing CRC map;
4. invoke the existing physical-graphics evidence collector after the MMC boot.

The retained evidence consists of the canonical local SHA-256/CRC32 list, the
RockOS transfer-and-`sync` transcript with credentials redacted, the U-Boot
size/CRC transcript, and the subsequent physical-graphics result.  A mismatch
fails closed; there is no automatic fallback to YMODEM.

## Verification

Existing `BoardSession` tests already cover MMC size/CRC validation and YMODEM
fallback behavior.  Add focused tests only if the physical-graphics wrapper
needs new arguments; do not build a second staging state machine.

The physical proof stages one current kernel and graphical Stage1 through
RockOS, observes a fresh U-Boot epoch, loads both through MMC, checks their
CRC32 values, and reaches the next Asterinas milestone.  Firefox acceptance
still requires the separate real HDMI, keyboard, mouse, serial, and recovery
evidence; successful staging alone makes no graphical claim.

## Expected Performance

The measured YMODEM path transferred a 3.1 MiB compressed kernel plus an
11.5 MiB compressed installer at roughly 50 KiB/s, costing about four to five
minutes per attempt.  Network staging pays that cost once and subsequent
`ext4load` operations use local eMMC.  Routine Firefox boots perform no rootfs
transfer, partition-2 write, or full 2 GiB hash because the installed root
identity is unchanged.
