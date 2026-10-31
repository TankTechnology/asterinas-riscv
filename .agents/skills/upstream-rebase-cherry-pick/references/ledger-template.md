# Pick ledger template

One row per feature, not per commit. Keep this file next to the rebase notes and
update it in the same commit as the pick/adaptation.

## Base

- Upstream: `<ref> <sha>`
- Rebase branch: `<branch> <sha>`
- Source branch: `<branch> <sha>`
- Date: `<YYYY-MM-DD>`
- Scope: `<QEMU Debian / desktop / filesystem / ...>`
- Exclusions: `<board/GPU/DRM/etc.>`

## Delivered or in progress

| Feature | Source commit(s) | Current commit(s) | Action | Dependency closure | Tests / evidence |
|---|---|---|---|---|---|
| Example: direct Debian gate | `fc78d33a8` | `fc78d33a8` | pick + adapt | `qemu-system-riscv64`, rootfs | `debian-direct-gate.log` |
| Example: ext2 durability | `31acb7c3c` | `31acb7c3c` | new/supersedes old | ktests | `2026-10-30-ext4-durability/` |

## Pending

| Feature | Source commit(s) | Required closure | Trial result | Blocker | Next action |
|---|---|---|---|---|---|
| Example: desktop shell gate | `a0d6f2938` | 24 modules; 18 missing | 8 conflicts when picked alone | gate framework not picked | pick bottom-up |

## Decisions

- `<source>` is **superseded** by `<current>`; reason: `<reason>`.
- `<source>` is **skip-out-of-scope**; reason: `<board/GPU/DRM>`.
- `<source>` is **blocked**; missing `<module>`; owner/next step.

## Status vocabulary

- `delivered`: behavior already present on the current line.
- `superseded`: old implementation replaced by a newer/current one.
- `picked`: source commit(s) replayed with provenance.
- `adapted`: source files/commits replayed with path/API/name changes.
- `blocked`: dependency closure incomplete; do not pick the top layer.
- `skip-out-of-scope`: not part of the approved workstream.

## Provenance conventions

```text
Picked-from: <source-sha>
Ported-from: <source-sha>
Source-tree: <old-branch>:<path>
```

For file-level picks, list the source branch, path, and the last commits that
touched it. For semantic ports, state the path/API change in the commit body.
