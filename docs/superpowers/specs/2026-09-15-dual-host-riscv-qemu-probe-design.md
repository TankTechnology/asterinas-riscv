# Dual-host RISC-V QEMU probe design

## Goal

Run one lightweight Asterinas RISC-V Stage1 probe on both the developer host
and the Megrez board's running RockOS, using the exact same kernel and
initramfs bytes. This is a virtual `virt`-machine test, not a Megrez hardware
test. Neither path requires KVM, a board reboot, a package install, a disk
image, a network device in the guest, or the Debian/Firefox rootfs.

## Chosen path

Add one host-side dual-run command, backed by the existing
`megrez_probe.qemu_probe_argv` and `classify_probe_transcript` protocol. It
accepts explicit kernel and Stage1 initramfs paths and runs the fixed
`boot,syscall213` selection with a fresh nonce. The local path starts QEMU
directly; the RockOS path invokes its installed `qemu-system-riscv64` over
SSH. Both QEMU processes use `virt`, Sv39-compatible CPU settings, 2 GiB RAM,
four vCPUs, `-nographic`, `-nic none`, and `-no-reboot`.

The RockOS path does not copy the repository or build there. It stages only
the two bounded artifacts in an isolated `/tmp/asterinas-qemu-probe/<identity>`
directory. A cached file may be reused only after RockOS verifies its full
SHA-256 and size; a changed artifact gets a new identity. Partial transfers
use unique temporary names and are promoted only after verification. SSH,
transfer, guest readiness, probe completion, and QEMU exit each have finite
deadlines. A remote `timeout` owns the QEMU lifetime, including when the
developer SSH connection fails. The command never opens `/boot`, changes
extlinux, mounts MMC, loads a module, or resets the board.

## Evidence and failure behavior

The two serial transcripts are retained independently, with one structured
result per host: host label, artifact SHA-256s, selected probes, nonce,
duration, last observed phase, QEMU exit status, and pass/fail reason. A pass
requires exactly one readiness record, the nonce-bound ordered PASS/DONE and
REBOOT_READY records for both probes, a requested guest reboot, and clean
QEMU exit. Panic, malformed or replayed records, hash mismatch, SSH loss, or
any deadline produce a failure with the bounded transcript. A RockOS TCG pass
must never be published as a physical input/display/MMC result.

## Simpler alternatives considered

Copying the full repository and the physical Megrez probe bundle to RockOS
would duplicate deployment assumptions and add large transfers. Building on
RockOS would repeat Cargo downloads and compilation. Switching RockOS to its
original KVM-capable kernel would require a reboot while nobody is beside the
board. None is necessary for this lightweight virtual probe.

## Acceptance

- Unit tests cover argument validation, exact artifact matching and cache
  reuse, remote command construction, timeout/cleanup, nonce and transcript
  rejection, and independent evidence paths.
- The same immutable artifact pair passes `boot,syscall213` on the developer
  host and on RockOS under QEMU/TCG, or each failure is independently retained
  with a precise phase and reason.
- A second RockOS run reuses its verified `/tmp` artifacts without a new
  transfer. RockOS remains on the same kernel and has no boot or partition
  changes.
- Documentation shows one short command for the routine dual-host probe and
  explicitly says Firefox and real devices remain separate gates.
