# RISC-V upstream line: current state and merge route

Date: 2026-10-31

## Snapshot

- Worktree: `/home/ubuntu/.config/superpowers/worktrees/asterinas/upstream-baseline`
- Validated code tip: `0d552997f` (tag `pre-main-promotion`).  The
  integration branch is fast-forwarded to the current line tip; docs-only
  commits sit on top of the validated code snapshot.  The remote
  `origin/rebase/upstream-20261029` has not been pushed.
- Tags: `pre-main-promotion` -> `0d552997f`;
  `backup/rebase-20261031` -> `01de8fc06`.
- Base: `upstream/main` = `cdd5bc109`.
- Validated code tip `0d552997f`: **68 ahead, 0 behind**
  `upstream/main`; the docs-only merge-route commit adds one more commit on
  top, still 0 behind.
- `origin/main` vs `upstream/main`: **6232 ahead, 4741 behind**; merge-base
  `634d4a501` (2022-11-04).
- `origin/rebase/upstream-20261029`: 36 commits behind the local line; no push.

Working tree is clean.  The old fork `main` and `origin/main` were not modified.

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

### 1. Freeze and preserve (done locally)

- Tag `pre-main-promotion` at the validated code tip `0d552997f`.
- Tag `backup/rebase-20261031` at the previous integration tip `01de8fc06`.
- Fast-forward local `rebase/upstream-20261029` to the current line tip
  (including this docs commit).
- **Waiting for authorization:** push the fast-forwarded integration branch to
  `origin/rebase/upstream-20261029`.

### 2. Promote the rebase line to the fork's new main

This is a deliberate one-time history replacement, not a merge:

1. Archive old `origin/main` as `archive/legacy-main-2026-10-31`.
2. Create `main-next` from the rebase line.
3. Run the gates on `main-next` (host suite, ktest, ext4 xfstests short list).
4. Set `main-next` as the fork default, then rename it to `main`.
5. Keep the old `main` read-only for historical reference; do not merge it into
   the new line.

This step needs explicit approval because it changes the fork's default
branch.  Until then, `feature/ext4-jbd2` / `rebase/upstream-20261029` is the
development line.

### 3. Upstreamable slices

Cut topic branches from `upstream/main`, not from the product line.  Do not mix
Debian rootfs, desktop gates, board drivers, or product evidence into these.

| Topic branch | Source | Status |
|---|---|---|
| `fix/ext4-mount-identity` | `73241d475` (name half) | ready to split |
| `fix/ext4-statfs-overhead` | `73241d475` (statfs half) | ready to split |
| `test/xfstests-riscv-ext4` | `66cd86630` + `13fe9f1af` | ready to split |
| `fix/proc-exe-interpreter` | new | needs implementation + ktest |
| `fix/ext4-journal-multidesc` | evidence candidate patch | needs recovery ktests + full-list triage |

Upstream PRs require explicit authorization.  The `/proc/self/exe` issue is
already recorded: Asterinas points it at the original script path rather than
the shebang interpreter.

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

- No push to `origin` or `private` until explicitly approved.
- No PR or push to `upstream/asterinas` under any circumstances without
  explicit authorization.
- No default-branch replacement or force-push without approval; the archived
  tags are the recovery path.
- No deletion of evidence; the two tags above preserve the previous tips.

## Immediate checklist

- [ ] Approve and push `rebase/upstream-20261029` fast-forward.
- [ ] Approve the `main-next` promotion and archive old `origin/main`.
- [ ] Split the three ready upstream topic branches.
- [ ] Use the evidence patch and logs to start the journal/full-list triage.
