---
name: upstream-rebase-cherry-pick
description: |
  Use when rebasing a downstream fork onto upstream, when cherry-picking commits
  from an old product branch onto a new upstream base, when deciding whether a
  feature is already present or superseded, when a cherry-pick conflicts or
  leaves broken imports, and when a pick ledger or dependency closure is needed.

  Triggers:
  - "rebase onto upstream, then pick our work"
  - cherry-pick from an old product branch or worktree
  - deciding which downstream commits are already on the new line
  - an old gate/tool file applies cleanly but cannot import its dependencies
  - a pick produces path, API, Makefile, or profile conflicts
  - keeping a ledger of source commit -> new commit -> evidence
---

# Upstream rebase cherry-pick

## Core model

```text
official upstream/main
        |
        +-- rebase branch (upstream base)
                |
                +-- feature-cluster picks, bottom-up
                        |
                        +-- ledger + tests + evidence
```

Never merge the old fork history and never replay the old branch wholesale.
The old and new trees share an old merge base and have different paths,
contracts, profiles, and boot flows. Treat the old branch as a **source of
commits and files**, not as a parent branch.

A clean cherry-pick is only a textual result. It is not proof that the feature
is runnable.

## 1. Freeze the base and inventory

Record before touching the tree:

- upstream ref and SHA;
- old/source branch and SHA;
- current rebase branch and SHA;
- `git status --short`.

List the current downstream stack:

```sh
git log --oneline --no-merges upstream/main..HEAD
git log --format='%an' --no-merges upstream/main..HEAD | sort | uniq -c
```

List candidate source commits by feature path or subject:

```sh
git log --oneline --no-merges OLD_BRANCH -- path/to/file
git log --oneline --no-merges OLD_BRANCH --since=YYYY-MM-DD
```

Create or update the pick ledger before picking. Use
`references/ledger-template.md`. A ledger row is one feature, not one commit.

## 2. Classify before picking

For every feature, choose exactly one action:

| Action | Meaning |
|---|---|
| `delivered` | Equivalent behavior already exists on the current line. |
| `superseded` | An old implementation exists, but the current line has a better/replacement implementation. Do not downgrade it. |
| `pick` | Self-contained; replay the source commit(s). |
| `adapt` | Replay a dependency cluster, then change paths/APIs/names. |
| `blocked` | Dependency closure is missing or unclear. Do not pick the top layer. |
| `skip-out-of-scope` | Board/GPU/DRM or otherwise outside the approved workstream. |

Check behavior and files, not commit hashes. A feature can be present under a
new name and a different implementation.

Record the reason for every `superseded` and `skip-out-of-scope` decision.

## 3. Compute the dependency closure first

Never start with the top-level gate or command. Resolve its local imports
transitively:

```sh
python3 .agents/skills/upstream-rebase-cherry-pick/scripts/local_import_closure.py \
  --branch OLD_BRANCH \
  --target tools.riscv.debian.rootfs.browser_web_qemu_gate \
  --current-tree .
```

The script prints the source-file closure and marks modules/files missing from
the current tree. It parses `tools.riscv.*` imports from source files, so it
works without checking out the old branch.

Order to pick:

1. self-contained leaf modules;
2. gate/runtime/protocol helpers;
3. contracts and rootfs/boot orchestration;
4. the gate or command the user asked for;
5. Makefile/docs/tests.

If a leaf module has no local imports, pick it first and add focused tests.

## 4. Trial-pick in a scratch worktree

Never trial a risky pick on the working branch. Use a detached temporary
worktree:

```sh
git worktree add --detach /tmp/asterinas-pick-trial HEAD
cd /tmp/asterinas-pick-trial
git cherry-pick --no-commit SOURCE_SHA
git status --short
```

If it conflicts, record the conflicted paths and abort:

```sh
git cherry-pick --abort || git reset --hard HEAD
cd -
git worktree remove --force /tmp/asterinas-pick-trial
```

If it applies cleanly, still check imports and tests before treating it as
usable:

```sh
python3 .agents/skills/upstream-rebase-cherry-pick/scripts/local_import_closure.py \
  --branch CURRENT_BRANCH \
  --target TOOL_MODULE \
  --current-tree .
```

Cleanup is mandatory even on failure; do not leave test worktrees or branches.

## 5. Merge handling rules

When a pick conflicts, resolve by intent, not by choosing "ours" or "theirs"
blindly.

| Old / source | Current / rebase |
|---|---|
| `kernel/src/...` | `kernel/core/src/...` (often a semantic port, not a pick) |
| old `desktop_m4_*` / `desktop_m5_*` names | current functional names; do not reintroduce milestone names |
| old multi-profile / Megrez / DRM dependencies | drop unless the current workstream explicitly needs them |
| old `rootfs/profiles.py`, `contract.py`, `signed_sources.py` | keep the current version; adapt imported call sites |
| old U-Boot/Stage1 flow | map to the current Stage1/direct-boot contracts |
| old Makefile variables | map to the current `test_riscv_debian_*` / product-gate variables |

Additional rules:

- Preserve current improvements. Never overwrite a newer current module with an
  older source version just because the old file is "the original".
- File-level picks are allowed when a source commit mixes unrelated changes:
  `git checkout OLD_BRANCH -- path/to/file`. Record the source commit(s).
- Split a mixed source commit into logical current commits. Do not import M4/M5
  history, board gates, GPU code, or dead diagnostics by accident.
- Add tests before or with the adaptation. A pick without import and behavior
  checks is not complete.

## 6. Commit records

Use a logical commit per feature cluster. Record provenance in the body:

```text
Picked-from: <source-sha-1>
Picked-from: <source-sha-2>
Source-tree: <old-branch>:<path>
```

For a semantic port:

```text
Ported-from: <source-sha>
Reason: kernel/src -> kernel/core/src and API changes
```

Update the ledger in the same change: source commit(s), current commit, status,
test/evidence path.

## 7. Consolidate after the feature gate is green

A pick is scaffolding until the target behavior is verified. Once the new gate
passes, reduce the code before moving on:

- If the current line's direct boot (`-kernel` + `-initrd` + `-dtb`) can drive
  the same guest, do not keep a generic U-Boot gate framework just because the
  source branch used one. Keep the smaller direct-boot path.
- Collapse sibling gates into one module with a milestone table instead of
  keeping M3/M4/M5 wrappers and their duplicated classifiers.
- Keep one parser/helper per concern. Do not keep two PPM parsers or two
  serial runtimes.
- Reuse the current line's existing gate/runner modules instead of importing a
  second generic framework.
- Keep one focused test module per remaining feature; delete tests for deleted
  wrappers.
- Record the removed source commits in the ledger. Git history and the ledger
  are the provenance; dead scaffolding does not need to stay in the tree.

Aim for the smallest production surface that still passes the real QEMU gate,
not for a one-to-one replay of the source branch.

## 8. Validation and evidence

After every cluster:

- focused unit tests for the picked modules;
- `make test_riscv_host HOST_TEST_JOBS=32` when host tests exist;
- the relevant QEMU gate (`test_riscv_debian_product`, direct, ext4, or the
  feature gate);
- preserve logs/JSON/rc receipts outside the repo exactly as for other work.

Do not push, open an upstream PR, or modify board/GPU state unless explicitly
authorized.

## Pitfalls seen in practice

| Pitfall | Concrete evidence |
|---|---|
| Clean apply but unusable | `0dfa6f615` (daily-use gate) applied cleanly to the rebase line, but its import closure was missing `desktop_m5_qemu_gate`, `gate_protocol`, `gate_runtime`, `rootfs_gate`, `rootfs_gate_backend`, and `systemd_m2_gate`. |
| Top-down pick | `a0d6f2938` (browser-web desktop-shell gate) conflicted on 8 paths because its gate framework and profiles were not picked first. |
| Kernel cherry-pick looks possible | Old kernel paths are `kernel/src`; current paths are `kernel/core/src`, and APIs were reorganized. Port semantically and record `Ported-from`. |
| Reintroducing removed verticals | Old M4/M5/Megrez/DRM modules pull board dependencies; keep only current functional names and approved scope. |
| Duplicating delivered work | Check current `git log`/files first; e.g. old ext4/JBD2 work is superseded by the current implementation. |
| Startup markers are not display evidence | The direct-boot desktop gate reached all six markers while the DTB framebuffer at `0x40000000` was an unassigned bochs PCIe BAR: `info pci` showed BAR0 at `0x0`, and `pmemsave` read back `0xffffffff`. A QMP screendump at the default VGA mode is not the guest framebuffer either. Boot with a RAM-backed simple-framebuffer DTB, validate dimensions/non-black pixels/colours from a monitor `pmemsave` dump, and require a guest-observed Firefox window. |
| Unprivileged guest output is not on the serial console | The Firefox wrapper's `printf` from uid 1000 can be dropped by the Asterinas console path. Use a root systemd observer that writes its own markers to `/dev/console` and include service/timeline diagnostics on timeout. |
| Basic-only boot still needs a network mode | Without `systemd.setenv=ASTERINAS_WEB_NETWORK_MODE=direct`, `browser-web-firefox` exits 64 with `invalid-network-profile`; `RestartPreventExitStatus=64` then leaves `asterinas-browser-web.service` failed and Firefox never starts. |
| `read` from a process substitution needs a newline | A helper that prints `id title` without a trailing newline makes `read` return 1 at EOF, so a visible Firefox window was reported as `FIREFOX_WINDOW=timeout`. Use `printf '%s %s\n'` and let the failure path diagnose the empty case. |
| An adjacent branch can use a different storage design | `948b2c634` looked like a flex_bg add-on but expected `stage_metadata`, `StagedGroupMetadata`, and a `desc_size` field from another branch; porting it was a rewrite, not a pick. Trial-record the design mismatch and keep the current implementation. |
| Additive conflict resolution is not a semantic port | `511143081` (punch hole) had only additive conflict hunks, yet the trial build produced 26 errors against current extent/indirect APIs. Always compile and run a focused test after resolving hunks, even when no semantic conflict remains. |
| Missing cleanup | Trial worktrees, cherry-pick state, and partial indexes must be aborted/removed before returning. |

When a new pattern appears, add it to this table and to
`references/case-2026-10-31-desktop-gate.md`, and extend the closure script if
the analysis needs a new check.

## References

- `references/ledger-template.md` — ledger columns and status vocabulary.
- `references/case-2026-10-31-desktop-gate.md` — the first concrete Asterinas
  rebase pick case, including closures, conflicts, and ordering.
- `scripts/local_import_closure.py` — source-branch import closure checker.
