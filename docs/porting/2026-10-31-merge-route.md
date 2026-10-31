# RISC-V upstream line: current state and merge route

Date: 2026-10-31

## Snapshot

- Worktree: `/home/ubuntu/.config/superpowers/worktrees/asterinas/upstream-baseline`
- Line tip: `feature/ext4-jbd2`, `rebase/upstream-20261029`,
  `origin/rebase/upstream-20261029`, and `origin/main-next` all point at the
  same line tip.  The validated code tip is `0d552997f`; docs-only commits
  (this file and its follow-ups) sit on top.
- Tags (local and pushed): `pre-main-promotion` -> `0d552997f`;
  `backup/rebase-20261031` -> `01de8fc06`.
- Fork default branch: `main-next`.
- Standalone upstream topic branch `fix/proc-exe-interpreter` at `1574b33e4`
  (based on `upstream/main`); pushed to the fork, not merged into the line and
  not submitted upstream.
- Archived old fork main: `archive/legacy-main-2026-10-31` -> `af48751df`
  (old `origin/main`).  The old `main` ref still exists at the same commit;
  it has not been deleted or renamed.
- Base: `upstream/main` = `cdd5bc109`.
- Validated code tip `0d552997f`: **68 ahead, 0 behind**
  `upstream/main`; the docs-only commits add a few more on top, still 0
  behind.
- Old `origin/main` vs `upstream/main`: **6232 ahead, 4741 behind**; merge-base
  `634d4a501` (2022-11-04).

Working tree is clean.

## Delivered on this line

| Commit | Scope |
|---|---|
| `66cd86630` | ext4 xfstests profile, short/full/block lists, RISC-V x3/x4 devices, host contract tests |
| `73241d475` | ext4 mount identity (`FileSystem::name()` reports `ext4`), journal + reserved-GDT `bsddf` accounting, ktests |
| `13fe9f1af` | cross-build host-bash shebang repair, `/dev/fd` alias, host baseline 100 tests |
| `dda543ad8` | green 8-case ext4 short-list record |
| `0d552997f` | 40-case full-list triage record |

Validation:

- ext4 xfstests short list: **8/8 pass**, `All conformance tests passed.`
- Host suite: **100 tests / 11 modules**.
- RISC-V ktest: **538 passed / 4 known failures** (baseline unchanged;
  `aster-core` 182/182).
- Full list: **not green**.  At 4 GiB a page-table OOM stops at generic/100;
  at 12 GiB 29 cases pass, then generic/224 reports a scratch inconsistency,
  generic/248 panics on task-spawn memory, and generic/006 needs the
  multi-descriptor journal candidate (saved, not committed).

Evidence:
`/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-31-xfstests-ext4-short/`.

## Why old `main` cannot be the merge target

`origin/main` and `upstream/main` diverged at a 2022 merge base.  A normal
merge of this 68-commit rebase line into `origin/main` would reintroduce the
old history and create a large, unreviewable conflict set.  The fork needs a
one-time line promotion instead.

## Merge route

### 1. Freeze and preserve (done)

- Tag `pre-main-promotion` at the validated code tip `0d552997f`.
- Tag `backup/rebase-20261031` at the previous integration tip `01de8fc06`.
- Fast-forward local `rebase/upstream-20261029` to the line tip and push it;
  push both tags to the fork.

### 2. Promote the rebase line to the fork's new main (in progress)

This is a deliberate one-time history replacement, not a merge:

1. Archive old `origin/main` as `archive/legacy-main-2026-10-31`
   (`af48751df`) and push it.
2. Create `main-next` from the rebase line, push it, and set it as the fork
   default (done).
3. The gates already ran on the same code tip (host suite, ktest, ext4
   xfstests short list).
4. Old `main` remains at `af48751df` for historical reference.  Do not delete
   or rename it until the new default has been stable and that step is
   explicitly approved.
5. Do not merge old main into the new line.

### 3. Upstreamable slices

Cut topic branches from `upstream/main`, not from the product line.  Do not mix
Debian rootfs, desktop gates, board drivers, or product evidence into these.

| Topic branch | Source | Status |
|---|---|---|
| `fix/proc-exe-interpreter` | new | **implemented** (`1574b33e4`): boot regression passes fixed / fails base; pushed to fork; upstream PR pending explicit authorization |
| `test/initramfs-devfd` | `13fe9f1af` subset | standalone generic test infrastructure |
| `test/xfstests-cross-shebang` | `13fe9f1af` subset | standalone generic test infrastructure (cross builds) |
| `fix/ext4-mount-identity` | `73241d475` subset | **blocked**: depends on the downstream ext4 extents/JBD2 feature stack, which is not in `upstream/main` |
| `fix/ext4-statfs-overhead` | `73241d475` subset | **blocked**: same dependency |
| `test/xfstests-riscv-ext4` | `66cd86630` | **blocked**: the profile needs kernel ext4 support; upstream x86 CI has no ext4 |
| `fix/ext4-journal-multidesc` | evidence candidate patch | product-line work; needs recovery ktests before any upstream consideration |

Upstreaming the ext4 extents/JBD2 feature stack itself is a separate, larger
series; the ext4 kernel and xfstests slices above become upstreamable only
after that decision.  Upstream PRs require explicit authorization.

The `/proc/self/exe` slice was validated on `upstream/main` + `1574b33e4`:
the base kernel reports `Unexpected /proc/self/exe: /test/boot_hello.sh`,
while the fixed kernel reports `PROC_SELF_EXE=/usr/bin/busybox` and
`Successfully booted.`  Evidence:
`/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-31-proc-self-exe/`.

### 4. Product line

Debian rootfs/Stage1, dsh, desktop/framebuffer gates, Megrez drivers, and board
work stay on the new main.  Future changes use short-lived branches from that
main and merge back with a squash PR.  Periodically rebase the product line on
`upstream/main`; do not reintroduce the 2022 fork history.

### 5. Remaining kernel work before full-list expansion

1. `fix/ext4-journal-multidesc`: add a multi-descriptor recovery ktest and
   validate a crash/replay path before committing.
2. generic/224: capture the `_check_generic_filesystem` detail and decide
   whether it is an ENOSPC/delalloc driver gap.
3. generic/248: separate a task/memory leak from the test's own memory demand;
   rerun with a larger guest and memory accounting if needed.
4. `/proc/self/exe`: fix the executable path to the interpreter and add a
   regression test.

## Authorization gates

- Fork pushes for `rebase`, `main-next`, the archive branch, and the two tags
  were authorized and completed.  Further pushes to `origin` or `private`
  need explicit approval.
- No PR or push to `upstream/asterinas` under any circumstances without
  explicit authorization.
- No deletion or renaming of the old `main` branch and no force-push without
  approval; the archived branch and tags are the recovery path.
- No deletion of evidence; the two tags above preserve the previous tips.

## Immediate checklist

- [x] Push `rebase/upstream-20261029` fast-forward.
- [x] Archive old `origin/main`, create/push `main-next`, and switch the fork
  default (old `main` retained).
- [x] Prepare and validate the `/proc/self/exe` standalone branch
  (`1574b33e4`); pushed to the fork.
- [ ] Prepare the `/dev/fd` and xfstests cross-shebang standalone branches;
  ext4-specific slices wait on the feature-stack decision.
- [ ] Use the evidence patch and logs to start the journal/full-list triage.
