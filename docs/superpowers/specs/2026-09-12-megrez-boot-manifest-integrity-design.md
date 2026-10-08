# Megrez Boot Manifest Integrity Design

## Goal

Prevent a Megrez reset from reaching U-Boot successfully but failing before
Asterinas because the persistent extlinux configuration names boot artifacts
that no longer exist. Preserve the existing fast-probe design: one host
command reuses verified MMC files, runs one bounded lightweight probe, and
leaves the board at a fresh U-Boot prompt. Firefox remains a separate,
explicitly selected workflow.

## Recorded Regression: `MEGREZ-BOOT-MANIFEST-001`

On 2026-09-12, a physical reset reached the U-Boot prompt but the automatic
Asterinas entry failed with:

```text
Retrieving file: /asterinas-sv48-fe1dcfdf7.booti
** File not found /asterinas-sv48-fe1dcfdf7.booti **
Skipping asterinas for failure retrieving kernel
```

`/extlinux/asterinas.conf` also referenced the absent
`/initramfs-full-712208ba4.cpio`. Both `asterinas.conf` and
`asterinas-canary.conf` contained those stale names even though partition 1
held several newer versioned artifacts. The failure happened before
`Enter riscv_boot`; it was not an Asterinas kernel, ext2, network, or Firefox
failure.

The retained evidence is:

- `target/network-stack-integration/physical/boot-baseline-after-manual-reset/serial.log`;
- `target/network-stack-integration/physical/boot-safe-mmc-baseline/serial.log`;
- `target/network-stack-integration/physical/boot-safe-mmc-baseline/boot.serial.log`.

The second experiment loaded the existing `current-safe` kernel, Stage1, and
DTB from MMC and matched each one against a local CRC32 identity. U-Boot read
the 14.8 MiB kernel in about 220 ms, so repeated download or MMC throughput was
not the startup bottleneck. That older kernel reached userspace preparation
but did not execute Stage1's first progress record. Its 180-second Asterinas
recovery deadline successfully produced a new OpenSBI/U-Boot epoch, and the
host stopped the following autoboot so the board remained at the U-Boot
prompt.

## Root Cause

The deployment and startup paths had two independent sources of truth:

1. host-side plans and bundles recorded the current artifact identities;
2. persistent extlinux files independently recorded artifact basenames.

Artifact deployment, configuration publication, and retirement of old files
were not one validated transaction. Nothing prevented a configuration from
remaining published after either referenced artifact was absent. The host
workflow also lacked a mandatory pre-boot check that parsed the selected
extlinux entry and proved every referenced file existed.

## Considered Approaches

### Selected: extend the existing fast-probe bundle and controller

Keep `current.json` as the host-side authority. Before touching the board, the
controller validates its schema and local identities. At U-Boot it checks the
exact MMC size and CRC32 of the three versioned files, boots one lightweight
probe with a fixed recovery deadline, requires a new firmware epoch, stops the
next autoboot, and exits with the board at the U-Boot prompt.

Persistent extlinux publication remains a maintenance operation. RockOS
installs versioned artifacts first and publishes the extlinux file last only
after size and SHA-256 verification. This approach reuses the existing
`BoardSession`, RockOS staging, and fast-probe contracts and adds no daemon or
second serial framework.

### Rejected: put the complete launch sequence in persistent U-Boot variables

This could reduce host orchestration, but it would move long artifact names,
CRC values, and recovery policy into SPI-backed environment state. Updating
that state requires `saveenv`, is harder to review and roll back, and would
create another source of truth.

### Rejected: upload every kernel and Stage1 for every run

YMODEM avoids reliance on persistent configuration, but repeatedly sending
the kernel makes the normal probe loop slow. TFTP is faster but still turns a
routine boot into a deployment and adds network availability to the kernel
probe precondition. Both remain explicit recovery transports, not the normal
path.

## Design

### One normal command

The existing fast-probe interface remains the intended entry point:

```bash
python3 -m tools.riscv.megrez_probe boot
```

The controller owns the complete run. It waits for a fresh U-Boot prompt,
prevents the stale automatic entry from running, validates the selected MMC
artifacts, boots one bounded probe, records its result, observes the Asterinas
software reset, interrupts the following U-Boot autoboot, and releases the
serial device. The successful terminal state is a live U-Boot prompt, not a
debug shell and not another automatically repeating probe.

The default remains a lightweight probe. Desktop and Firefox tests must be
selected through their dedicated launchers and may use a longer deadline.
They are not added to the fast-probe command.

### Manifest invariant

A boot configuration is publishable only if all of these statements are true:

- the kernel, Stage1 initramfs, and DTB use immutable versioned basenames;
- the host source size, SHA-256, and CRC32 match the deployment bundle;
- RockOS reports the same size and SHA-256 after `sync` on partition 1;
- the extlinux configuration names those exact three basenames;
- every named file exists on partition 1 before the configuration is renamed
  into its final path;
- U-Boot observes the expected size and CRC32 before the first boot;
- an old artifact cannot be retired while any published configuration still
  references it.

`current.json`, the extlinux configuration, and its three files form one
logical generation. Publication writes a temporary configuration, validates
it, synchronizes it, and atomically renames it last. Failure leaves the
previous generation bootable. Routine probes do not boot RockOS and do not
rewrite either partition.

### Failure behavior

Validation fails closed before `booti`. Missing files, wrong sizes, CRC
mismatches, stale configuration references, serial ownership conflicts, and
unknown probe names produce a structured failure without trying another
kernel automatically.

After Asterinas starts, the software recovery deadline is always armed. The
host waits for one new OpenSBI/U-Boot epoch and interrupts its autoboot. If the
new epoch does not appear within the bounded recovery window, the result is
`manual-reset-required`; the controller does not issue speculative commands
to an unknown board state.

The persistent U-Boot environment is not changed in this milestone.
`bootdelay=30` remains in place while the repaired path is being qualified.
Reducing it would require a separate decision and physical proof because it
writes SPI-backed environment state and shortens the operator recovery window.

### Partition boundary

Only RockOS may perform the one-time partition-1 maintenance transaction.
Asterinas never writes partition 1 to update its own boot configuration.
Partition 2 is not read or written by the lightweight probe. A browser run may
mount the already-installed Debian root under its existing explicit policy,
but that is outside this repair.

## Verification

The regression must be prevented at three levels:

1. Unit tests parse representative extlinux configurations and reject a
   missing kernel, missing initramfs, missing DTB, mutable basename, identity
   mismatch, duplicate generation, and retirement of an artifact still in
   use.
2. A QEMU test proves that a deliberately stale extlinux generation fails
   before boot, while a valid generation runs one lightweight probe, performs
   bounded software recovery, and leaves a fresh U-Boot prompt.
3. One physical Megrez run uses already installed MMC artifacts, verifies
   their size and CRC32 without uploading them, reaches the probe completion
   marker, observes a new firmware epoch, and leaves the board at the U-Boot
   prompt.

The physical run is one epoch, not three. Three-cycle stability qualification
remains a separate release gate. The physical acceptance result must record
the exact generation identity and the absence of partition writes and serial
artifact transfer.

## Completion Criteria

This milestone is complete when:

- `MEGREZ-BOOT-MANIFEST-001` has an automated failing fixture and cannot
  regress silently;
- the normal probe invocation needs only the probe name;
- a routine probe performs no build, download, RockOS boot, partition write,
  U-Boot `saveenv`, or Firefox startup;
- the verified MMC generation reaches the lightweight probe's terminal marker;
- software recovery returns the board to a fresh U-Boot prompt and the host
  stops the next autoboot;
- the stale persistent extlinux entry is repaired through one reviewed,
  rollback-safe RockOS maintenance transaction only after software tests pass.
