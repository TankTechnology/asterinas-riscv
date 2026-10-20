# Stable Desktop Display and Recoverable Hardware Validation

**Status:** Proposed design for review
**Scope:** Asterinas desktop bring-up on QEMU and the EIC7700 development board
**PowerVR:** Explicitly out of scope for this phase

## Goal

Make the software framebuffer and DRM/KMS display paths independently testable,
recoverable, and suitable for daily desktop use. The same gates must establish
correctness in QEMU before a bounded real-board run is allowed. A failed board
probe must return to RockOS or U-Boot through a hardware watchdog or an
authenticated software reboot; it must never leave an operator without a
known recovery path.

The work also consolidates the boot scripts and their evidence format. Every
boot cycle will have one owner for the serial device, one nonce, one selected
boot entry, one bounded experiment, and one recovery result.

## Non-goals

- PowerVR firmware, GPU command submission, GPU memory management, or Mesa
  acceleration.
- Firefox WebRender acceleration. Firefox remains on the software/fbdev path
  until DRM/KMS scanout is independently qualified.
- A physical power-cut durability claim for ext2 or SD media.
- A full replacement of the existing desktop or networking stack.

## Integration boundary

The current branch contains a large set of pre-existing desktop, network,
hardware, and evidence changes. The integration branch will be based on the
remote `main` and will absorb changes in evidence-sized bundles rather than
merging the divergent histories wholesale.

The first bundle contains the BIO completion ordering fix (`00481890d`), the
RISC-V trap/CPU-local fixes with their tests, and the QEMU desktop startup
changes that already have passing focused gates. The ext2 metadata PR is
reviewed as two commits (`784f7d750360a7bf412cda058cfae28afc89f414` and
`50571997c921a220b24ec11ce3efee0f0366e5da`) and is accepted only if its
focused concurrent-write and offline `e2fsck` gates pass on the integration
base. The large ext4 journal PR and experimental PowerVR commits remain
separate until they have their own reviewed acceptance criteria.

No merge operation will include the current worktree's unrelated unstaged
files. Each bundle is cherry-picked into a clean integration worktree, built,
tested, and recorded with input hashes before it is fast-forwarded to the
project main branch.

## Recovery contract

The physical runner is a small state machine shared by the QEMU and board
gates:

```text
RockOSVerified
  -> WatchdogArmed
  -> EntrySelected
  -> AsterinasBooting
  -> ExperimentBounded
  -> AsterinasStopping
  -> UBootObserved
  -> RockOSVerified
```

The only terminal states are `Recovered` and `FailedWithRecoveryEvidence`.
`FailedWithRecoveryEvidence` means the experiment stopped, the watchdog or
software reboot was observed, and a fresh nonce challenge verified RockOS or
U-Boot. A timeout before recovery is an operator-visible failure and blocks
the next board experiment.

The runner enforces these invariants:

1. The watchdog is armed before selecting an Asterinas entry and its deadline
   is shorter than the experiment's maximum unresponsive interval.
2. A physical probe executes at most once per boot cycle. A register timeout
   stops the cycle and is never retried automatically.
3. The serial device has a single owner. Before handoff and after recovery,
   the runner closes and reopens it, then verifies a fresh nonce, UID 0, and
   boot identity.
4. No experiment may replace a working recovery path or modify the default
   RockOS boot entry.
5. All commands, selected artifacts, watchdog deadline, serial identity, and
   recovery observations are recorded without credentials.

The common implementation will live behind one control-gate interface used by
`tools/riscv/megrez_board_session.py`, `megrez_desktop_boot.py`, and the
hardware probe commands. Existing command-specific scripts become thin
profiles rather than independent boot protocols.

## Display architecture

### Layer 1: software framebuffer

The fbdev path remains the safe fallback and the first acceptance target. The
guest publishes a fixed test pattern using the exact reported width, height,
stride, bytes-per-pixel, and pixel format. It then reads the first 4 KiB,
writes a small region, restores the original bytes, and emits a checksum.

The gate validates both sides of the boundary:

- Guest: `FBIOGET_VSCREENINFO`, `FBIOGET_FSCREENINFO`, mmap bounds, write/read
  checksum, and at least three distinct colors.
- QEMU host: screendump or display backend capture contains the same pattern
  and at least three colors.
- Recovery: the guest remains responsive and completes the normal reboot gate.

Pixel packing, stride arithmetic, clipping, and buffer bounds will be moved
behind small pure functions so they can be tested without booting an OS.

### Layer 2: QEMU DRM/KMS

After Layer 1 is green, QEMU will expose a virtio-gpu DRM device. The minimum
contract is `/dev/dri/card0`, a connector and mode in `/sys/class/drm`, a dumb
buffer, mmap, a modeset, one page flip, one vblank/fence completion, and clean
buffer release. The fbdev path remains available as a fallback.

The gate will fail closed if a fence is missing, if a released buffer is still
selected for scanout, or if host and guest pixel evidence disagree. Firefox is
not part of this gate.

### Layer 3: real-board scanout without PowerVR

The first board run uses only the already known framebuffer/display resource.
It collects the device-tree node, MMIO range, clocks, reset lines, IRQs,
format, stride, and mode without touching GPU registers. A second run performs
one bounded fb0 write/read/restore cycle. A third run, only after the first two
recover cleanly, checks DRM/KMS resource discovery and scanout state.

Because no HDMI capture device is currently available, an automated physical
pixel claim requires a display-controller CRC or equivalent scanout counter.
If the board exposes neither, the gate records kernel-side checksums and marks
the external-panel observation as requiring one human confirmation; it will
not report visible pixels as machine-verified.

## Correctness and formal evidence

The display core will have a pure state model with:

```text
Buffer: Free -> Mapped -> Drawn -> Flushed -> ScanningOut -> Released
Fence:  Pending -> Signaled | Failed
```

The model will exhaustively explore all event sequences up to a fixed bound and
check these invariants:

- scanout never references `Free` or `Released` storage;
- a buffer cannot be released before its fence is `Signaled` or `Failed`;
- a failed fence cannot be treated as a successful page flip;
- completion is published only after the callback has made buffer contents
  visible to waiters.

The model is accompanied by Rust unit tests for packing and bounds and a small
proof artifact for the transition invariants. Kernel integration tests then
exercise the same order through QEMU. This is a bounded refinement argument,
not a claim that the entire kernel is formally verified.

The existing BIO ordering regression remains the reference pattern: first make
the old ordering fail, then make the minimal ordering change pass, then run the
QEMU rootfs gate. The same red/green/evidence sequence is required for buffer
ownership and page-flip changes.

## Startup simplification

The current boot flow is split across several scripts with overlapping serial,
watchdog, nonce, and recovery logic. The consolidation will:

1. define one `BootCycle` record containing artifact hashes, selected entry,
   nonce, watchdog deadline, experiment name, and recovery state;
2. use one `run_bounded_cycle()` implementation for QEMU and the board;
3. expose profiles only for the device-specific command list;
4. centralize timeout, serial reopen, nonce challenge, and recovery parsing;
5. keep the Asterinas root debug console opt-in and recreated on every selected
   boot; and
6. make failure artifacts sufficient to decide whether another cycle is safe.

This removes duplicated retry logic and makes “timeout, stop, recover” the only
physical error path.

## Acceptance ladder

| Stage | Required evidence | Blocks |
| --- | --- | --- |
| Integration baseline | Clean build, focused unit/ktest, rootfs QEMU boot | All later stages |
| QEMU fbdev | Guest and host multi-color pattern, checksum, reboot recovery | QEMU DRM |
| QEMU DRM/KMS | Card/connector/mode, dumb buffer, flip/fence, fallback | Board display |
| Board fb0 | Watchdog, nonce, bounded write/restore, RockOS recovery | Board DRM |
| Board DRM/KMS | Read-only resources, then one bounded scanout cycle, recovery | Firefox DRM |
| Firefox software path | Browser startup and fixed local page on qualified scanout | Acceleration |

Any failed stage freezes the next stage. There is no automatic escalation from
QEMU to the board or from fbdev to DRM.

## Stop conditions and risks

- Any board serial loss, watchdog expiry without recovery evidence, or MMIO
  timeout stops physical work until the recovery chain is re-established.
- A QEMU host screenshot that remains single-color is a failed scanout gate,
  even when guest framebuffer checks pass.
- A clean `e2fsck` after a normal shutdown is not evidence of sudden-power-loss
  durability; ext2 remains subject to that limitation.
- Unsupported systemd syscalls are recorded separately from filesystem and
  display correctness so they do not mask a lower-layer failure.
