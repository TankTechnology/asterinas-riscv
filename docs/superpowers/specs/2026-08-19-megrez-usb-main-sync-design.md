# Megrez USB Keyboard Main Synchronization Design

## Objective

Update the published `codex/megrez-usb-keyboard` topic branch in
`TankTechnology/asterinas-riscv` to the current `origin/main` while preserving
its existing public history. Resolve the branch conflict, retain the validated
Megrez USB keyboard behavior, and produce a locally verified merge result that
can update pull request #1 without a rebase or force-push.

The starting refs recorded on 2026-08-19 are:

- Topic branch: `243edb99b` (`codex/megrez-usb-keyboard`).
- Downstream main: `1ed8a46c5` (`origin/main`).
- Merge base: `09dcf1e63`.
- Divergence: 31 topic-only commits and 129 main-only commits.

## Repository and Isolation

All work is performed in the existing clone whose `origin` is
`TankTechnology/asterinas-riscv`. The upstream `asterinas/asterinas` remote is
not an integration target for this task.

The merge is prepared on a new `codex/` synchronization branch in an isolated
Git worktree. The currently checked-out topic branch and its untracked logs,
caches, and worktree metadata are not modified. The synchronization branch is
created from the topic branch after this design document is committed, so the
final merge remains a fast-forward update of the published topic branch and
preserves the 31 pre-existing implementation commits.

## Integration Strategy

Merge `origin/main` into the synchronization branch with a normal merge commit.
Do not rebase, squash, cherry-pick the 31-commit topic history, or rewrite any
published commit.

The merge must preserve the current downstream features that have landed since
the topic branch last synchronized, including the NixOS foundation work, DRM,
virtio-sound, browser, networking, and Linux ABI fixes. USB-specific behavior
is reapplied only where the main-side implementation does not already provide
it. Unrelated changes from `track/nixos`, `track/drm`, or `integration` are not
introduced directly; only changes already present on `origin/main` participate
in this merge.

## Conflict Resolution Policy

The merge preview identifies seven paths changed on both sides:

- `.gitignore`
- `Makefile`
- `kernel/comps/input/src/event_type_codes.rs`
- `kernel/comps/uart/src/console.rs`
- `ostd/src/arch/riscv/mm/eic7700_cache.rs`
- `tools/riscv/eic7700_isolation.sh`
- `tools/riscv/prepare_qemu_uboot_booti.sh`

Resolution follows these rules:

1. Use a union for independent ignore entries and Makefile targets, while
   rejecting duplicate targets or stale aliases.
2. Preserve the main branch's expanded input, UART, and console contracts.
   Reapply only the USB keyboard event translation and serial-console injection
   behavior required by the topic branch.
3. Preserve the main branch's EIC7700 compatibility guards and validation
   semantics. Retain topic-side changes only when they are still required by
   the Megrez USB DMA or isolation path.
4. Preserve newer main-side boot-profile and artifact handling in the RISC-V
   scripts. Reintroduce Megrez-specific profile arguments without weakening
   validation, cleanup, or evidence checks.
5. Regenerate dependency metadata through Cargo when manifest integration
   changes the resolved graph; do not hand-edit conflicting lockfile entries.
6. Do not resolve conflicts by taking an entire side when that would discard an
   independently valid behavior from the other side.

## Validation

Validation is staged so cheap structural checks run before QEMU workloads:

1. Confirm the merge contains both parents, has no unmerged paths, and changes
   only the expected union of `origin/main` and the topic branch.
2. Run formatting, whitespace, license, and relevant Cargo metadata checks.
3. Build the RISC-V kernel and check the `ostd`, `aster-pci`, and `aster-usb`
   dependency graph on the pinned toolchain.
4. Run the USB keyboard oracle tests and the Megrez board-session Python tests.
5. Run the relevant kernel tests, including OSTD DMA/USB coverage and the
   kernel TTY/keyboard path. Kernel tests and normal builds run serially because
   `cargo osdk test` overwrites the normal kernel artifact.
6. Run the RISC-V QEMU keyboard regression, including normal keys, modifiers,
   control keys, rapid input, single registration, and zero panic.
7. Run the Megrez Sv48/Svade contract simulation through
   `tools/riscv/verify_megrez_sim.sh` and verify the userspace marker.
8. Exercise the invalid-DWC3-selection case and confirm fail-safe fallback with
   no panic.
9. Run broader repository checks in proportion to available time and record any
   failure as introduced, inherited from `origin/main`, environmental, or
   infrastructure-related.

Physical Megrez DWC3 interaction is not claimed as verified without board
access. Existing physical-board evidence remains historical until the board
procedure is rerun.

## Failure Handling

If a validation failure is caused by the merge, fix it on the synchronization
branch and add or retain the narrowest regression test that demonstrates the
failure. If a failure reproduces unchanged on `origin/main`, record it as a
baseline failure rather than altering unrelated code. If the USB behavior
cannot be reconciled without redesign, stop before updating the published topic
branch and document the blocking interface change.

## Publication Policy

The synchronization work is committed locally with a normal merge commit and
any required focused follow-up commits. No remote branch is pushed until the
resulting commit list, diff, and validation evidence have been reported for
review. Publication, when approved, is a normal fast-forward push to
`origin/codex/megrez-usb-keyboard`; force-push is prohibited.

## Success Criteria

The task is complete when:

- The synchronized branch contains current `origin/main` and all 31 existing
  topic commits without history rewriting.
- Git reports no merge conflict and pull request #1 is no longer conflicting
  when the reviewed result is published.
- The USB keyboard, RISC-V PCI/xHCI, DMA, TTY, Megrez tooling, and board
  handoff documentation remain present and internally consistent.
- Required build, unit, QEMU keyboard, Megrez contract, and DWC3 fail-safe
  gates pass, or any external baseline limitation is explicitly evidenced.
- The current working tree remains untouched apart from this committed design
  document until the isolated implementation worktree is created.

## Non-goals

- Rebasing or squashing the published topic history.
- Merging `track/nixos`, `track/drm`, or `integration` directly.
- Fixing unrelated open CI failures from other workstreams.
- Claiming current physical-board verification without rerunning it.
- Pushing or force-pushing a remote branch without review.
