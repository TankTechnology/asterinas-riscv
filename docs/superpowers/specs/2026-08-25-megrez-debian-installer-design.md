# Megrez Asterinas Debian Installer Design

## Goal

Install the frozen Debian RISC-V ext2 image into the exact Megrez SD-card
partition 2 using Asterinas itself, with bounded progress and safe restart after
an interruption.

## Boundary

RockOS may copy one immutable installer initramfs into the boot filesystem. It
must not write partition 2, mount the Debian target, or serve as the runtime
acceptance path. U-Boot loads the Asterinas kernel and installer initramfs;
Asterinas performs every target read, write, sync, and verification operation.

The kernel remains read-only unless the existing
`asterinas.mmc_write_partition2` boot argument is present. The installer also
requires an exact root-image SHA-256 boot argument, so accidentally booting the
archive cannot start installation.

## Archive

`megrez_installer.py build` accepts a trusted raw `newc` base initramfs, the
frozen Debian image, its manifest, and package lock. It validates the frozen
root contract, parses the base archive without extracting it, replaces `/init`,
and adds an installer manifest plus independently compressed 32 MiB chunks.
All paths, modes, ownership, timestamps, ordering, and gzip headers are
deterministic. Publication is a same-directory temporary file followed by
`fsync` and atomic replacement.

## Runtime protocol

The generated `/init` mounts proc, sysfs, and devtmpfs, validates the exact p2
size (4 GiB), validates every compressed chunk before the first write, and then
processes chunks in order. For each chunk it hashes the current target range,
skips an already-correct range, or decompresses and writes exactly that range.
It syncs and reads the range back before emitting a progress marker. A failure
emits one terminal failure marker and holds PID 1 alive. Completion performs a
full 1 GiB target hash, emits one success marker, and holds without rebooting.

## Evidence and tests

Host tests cover strict `newc` parsing, traversal/duplicate rejection,
deterministic chunk metadata, exact init protocol, atomic failure preservation,
and a small-image resume simulation. The real gate records installer/archive
hashes, all per-chunk markers, the final target hash, and a subsequent Stage1
boot from the installed partition.

