# Megrez Selectable Boot Menu Design

## Goal

Make a routine Megrez boot a local board operation: reset or reboot, choose a
mode from one menu, and boot already verified files from MMC. The normal path
must not rebuild the kernel, upload artifacts, calculate ad-hoc CRC values, or
require a sequence of U-Boot commands.

The menu provides four modes:

1. RockOS;
2. Asterinas Basic;
3. Asterinas Probe;
4. Asterinas Desktop.

RockOS is the default after ten seconds without operator input. The full
Debian desktop and Firefox remain an explicit mode rather than becoming a
precondition for ordinary Asterinas development.

## Existing Contracts

The implementation extends, rather than replaces, the boot-manifest integrity
workflow. Kernel, Stage1 initramfs, and DTB files retain immutable versioned
names and recorded size, SHA-256, and CRC32 identities. RockOS remains the only
environment allowed to publish files on partition 1. Asterinas does not update
its own boot files.

The following safety properties remain unchanged:

- the persistent U-Boot environment is not modified and `saveenv` is not used;
- the vendor `/extlinux/extlinux.conf` is not edited;
- artifacts are installed before a configuration that references them;
- a configuration is published last with an atomic rename;
- routine boots do not transfer artifacts over serial or the network;
- partition 2 is not rewritten by the boot-menu workflow.

The existing U-Boot chain remains:

```text
bootcmd_asterinas -> /extlinux/asterinas.conf
                 -> bootcmd_rockos on selector-load failure
                 -> bootcmd_factory on RockOS-load failure
```

This keeps the vendor RockOS configuration as an independent recovery path.

## Considered Approaches

### Selected: one generated extlinux selector

Use `/extlinux/asterinas.conf` as a unified menu. At publication time, read the
currently installed vendor configuration and preserve its normal RockOS boot
stanza, then add the three Asterinas stanzas. This works with the existing
`bootcmd_asterinas` value and does not add persistent U-Boot state.

The cost is a small, strict renderer for the selector. It is justified because
hard-coding a copy of RockOS paths would drift when RockOS is upgraded. The
renderer accepts only the expected extlinux structure and fails closed instead
of guessing when the vendor format changes.

### Rejected: persistent U-Boot `bootmenu` variables

This would put the selection before extlinux, but it requires `saveenv` and
duplicates artifact paths in SPI-backed state. It is harder to update,
inspect, and recover than a file on partition 1.

### Rejected: separate configurations selected by host scripts

Keeping independent files and driving `sysboot` over serial is useful for
recovery, but it does not give an operator a board-local selector. It also
preserves the long command-driven workflow that this milestone is intended to
remove.

## Architecture

### One selector, shared artifacts

All Asterinas entries reuse the same selected kernel and DTB generation. Basic
and Probe reuse the same lightweight Stage1; only their init arguments differ.
Desktop reuses the same kernel, DTB, identity checks, publication transaction,
and serial evidence format, but selects the existing full Debian Stage1 and
desktop boot arguments.

The resulting artifact set is:

```text
verified kernel + verified DTB
                + lightweight Stage1 -> Basic / Probe
                + full Stage1        -> Desktop
```

An artifact is prepared and published only when its content changes. Choosing
a mode never triggers a build or upload.

### Menu entries

**RockOS** copies the normal, currently selected RockOS stanza from the vendor
configuration. It is the selector default after ten seconds. Rescue and other
vendor-specific entries remain available through the untouched vendor menu
and are not duplicated into the Asterinas selector.

**Asterinas Basic** boots the lightweight Stage1 into the ordinary interactive
base userspace. It has no automatic reboot deadline because it is a normal
development environment.

**Asterinas Probe** boots the same lightweight Stage1 with the probe init mode
and a bounded recovery deadline of 90 seconds. It emits the
existing structured probe records and then requests a reboot. The next menu
defaults to RockOS unless an operator selects another entry.

**Asterinas Desktop** boots the existing full Debian/Firefox environment. It
uses the already validated desktop root-init and root-filesystem policy. It
has no short automatic reboot deadline and does not add Firefox to Basic or
Probe.

### Manifest evolution

The manifest authenticates the exact selector bytes and every referenced
Asterinas artifact. Schema version 3 adds named boot modes so that Basic and
Probe can intentionally share artifact identities while using different boot
arguments, and Desktop can select the full Stage1. Existing single-entry
schema-v2 bundles remain readable for recovery and historical evidence; only
schema version 3 is publishable as the selector.

The RockOS stanza is recorded as part of the exact selector identity, but
RockOS files are owned by the vendor installation rather than copied into the
Asterinas artifact generation.

### Operator workflow

Routine operation is entirely board-local:

```text
reset or reboot -> wait for menu -> select one entry -> boot from MMC
```

A single host-side maintenance entry point owns selector rendering,
validation, canary publication, final publication, and qualification. It may
reuse the current manifest, RockOS attestation, board-session, probe, and
desktop controllers internally. Those remain implementation modules rather
than separate manual steps presented to the operator.

## Publication and Failure Handling

Publication runs once in RockOS and follows this order:

1. read the vendor extlinux configuration and select its normal default
   RockOS stanza;
2. load the chosen Asterinas manifest and verify every local artifact;
3. render the selector and parse the rendered bytes again;
4. verify that every referenced Asterinas file exists on partition 1 with the
   expected size and SHA-256;
5. write and synchronize a versioned canary selector;
6. read the canary back and verify its exact identity;
7. qualify the canary on the physical board;
8. atomically rename the already qualified bytes to
   `/extlinux/asterinas.conf`.

Any parse error, missing file, identity mismatch, unexpected RockOS structure,
or serial ownership conflict stops before final publication. The previously
published selector is left unchanged.

If U-Boot cannot load the selector at reset, the unchanged command chain tries
the vendor RockOS configuration. If a selected Asterinas kernel completely
hard-locks, its software recovery timer cannot execute; this remains
`manual-reset-required`. Hardware-watchdog integration is a separate
milestone, rather than hidden complexity in the menu change.

Basic and Desktop stay running until an operator requests shutdown or reboot.
Only Probe has a bounded automatic recovery policy.

## Verification

### Software gates

Host-side tests cover:

- extracting the default RockOS stanza without rewriting vendor bytes;
- rejecting a missing or ambiguous RockOS default;
- rendering and parsing all four menu entries;
- enforcing RockOS as the ten-second default;
- proving Basic and Probe share lightweight artifacts but have distinct init
  arguments;
- proving Desktop selects the full Stage1;
- rejecting missing, mutable, mismatched, or unrecorded Asterinas artifacts;
- reading existing schema-v2 manifests while refusing to publish them as a
  multi-mode selector;
- proving a failed canary never replaces the active selector.

Existing QEMU gates exercise the lightweight Basic/Probe user-space paths and
the full Debian root-init path. The physical U-Boot menu itself is qualified on
Megrez rather than treated as proven by a different virtual firmware.

### Physical qualification

Physical tests may use multiple boots. They reuse one frozen artifact
generation and one captured test plan; they do not rebuild or upload between
cycles.

Qualification proceeds in stages:

1. load a canary selector explicitly, without replacing the active selector;
2. verify menu rendering, serial-console selection, and a ten-second default
   RockOS boot;
3. repeat the RockOS default and fallback path for at least three successful
   cycles;
4. boot Basic to its interactive shell for at least three successful cycles;
5. run at least three Probe cycles, requiring structured completion records,
   a new firmware epoch, and recovery to the selector each time;
6. boot Desktop to Debian, the graphical session, and Firefox for at least two
   successful cycles;
7. prove that a deliberately invalid candidate is rejected without replacing
   the active selector;
8. publish the qualified selector, then perform one software-reboot test and
   one operator-observed cold-start test.

Every cycle records the selected mode, manifest identity, artifact identities,
serial log, boot-stage timestamps, terminal condition, partition writes, and
whether any transfer occurred. A failure stops blind repetition: the next run
must test a specific hypothesis derived from the captured stage and error.

The final successful state is RockOS, leaving the board remotely maintainable.

## Completion Criteria

This milestone is complete when:

- a reset or reboot displays the four-mode selector;
- ten seconds without input boots RockOS;
- routine mode selection performs no build, upload, checksum preparation,
  U-Boot command sequence, `saveenv`, or partition-2 rewrite;
- Basic, Probe, and Desktop reuse the same verified kernel and DTB;
- Basic and Probe reuse the same lightweight Stage1;
- the physical repetition thresholds pass using one frozen artifact
  generation;
- a bad candidate cannot replace the working selector;
- selector-load failure retains the vendor RockOS fallback;
- the tested selector is published atomically with auditable evidence.

## Non-goals

This milestone does not merge the network stack, debug Firefox networking,
add DRM acceleration, alter the RockOS vendor configuration, change persistent
U-Boot environment variables, write partition 2, or add a hardware watchdog.
Those changes require their own evidence and must not complicate the basic
boot selector.
