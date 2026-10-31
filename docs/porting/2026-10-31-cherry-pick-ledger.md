# Downstream pick ledger (2026-10-31)

## Base

- Upstream: `upstream/main` = `cdd5bc10907f752dc19a399297ea1fd57af20ee4`
- Rebase branch: `rebase/upstream-20261029` = `feature/ext4-jbd2`
- Source product branch: `feature/qemu-network-runtime` = `af48751df`
- Scope: QEMU-supported Debian/RISC-V surface. Board/GPU/DRM work remains out
  of scope unless separately approved.
- Current stack: the downstream commits over upstream, including the P0
  desktop-evidence, P1 broader-ext4, fresh-builder evidence, and cleanup
  changes.  Use `git log upstream/main..HEAD` for the exact set.

This ledger records the provenance of the current stack and the dependency
closure for the still-missing desktop/browser automation. It is updated in the
same change as each new pick. See
`.agents/skills/upstream-rebase-cherry-pick/SKILL.md` for the procedure.

## Delivered on the rebase line

| Feature | Representative source commit(s) | Current commit(s) | Action | Evidence |
|---|---|---|---|---|
| virtio-net 10-byte header | `ce2cf1662` | `5cf47ed96` | semantic port | `direct`/network gates |
| unprivileged port-zero connect | old product tree | `56218c47c` | new on rebase line | ktest + dsh |
| SCM_RIGHTS UNIX-socket passing | `79ee67872`,`f901a9f0f` | `f3830f7cb`,`d92b6530a` | semantic port + test | Firefox content process |
| klog, `/dev/kmsg`, boot identity, `syslog(2)` | old klog work | `415c233e4`,`978d0413b` | port/new | journald, `/dev/kmsg` probes |
| devtmpfs, `/dev/shm`, devpts/PTY | `3604162f8` and related | `977757632`,`2f42172df` | port | PTY probes |
| framebuffer graphics class + DT simple-framebuffer | `0ce8ab960` and related | `bd966a253`,`6cea7ade3` | port | desktop framebuffer evidence |
| rtnetlink set, IPv4 routes, `/proc/net/{dev,route,tcp,udp}`, netns | `ef469eb48`,`779667a03` | `fb837cf21`,`f5febeb7a`,`e9375313c`,`b32616377`,`980e47646` | port | QEMU network/procfs evidence |
| Debian rootfs/Stage1/direct gate/Image sidecar | old builders and gates | `fc78d33a8`,`25909f303`,`0881283db` | port/adapt | rootfs contract, direct gate |
| ext4 extents + JBD2 + crash gates | `294c225ce` | `30cd05424`,`7b2bfc87d`,`7c960b105` | superseded/expanded | 64-bit and crash gates |
| ext2 durability invariants | old ext2 work | `31acb7c3c` | port/new | ktests + ext2/e2fsck probe |
| host suite + aggregate product gate | old host suite, not cherry-picked wholesale | `2abf63a3a` | new on rebase line | 90 host tests, product receipt |
| Desktop direct-boot gate (kernel + Stage1 initrd + RAM-fb DTB, ordered session markers, visible Firefox, guest framebuffer pixels) | new on rebase line; old gate behavior used as reference | `805fb3010` | new + consolidated | `test_desktop_gate.py`, `desktop_frame_evidence.sh`, fb5 `result.json`, `firefox.png` |
| Broader ext4 matrix (200-file directory, 20-level tree, hole reads, random SHA-256, `fallocate` zero-range, generated 64-bit image) | old feature commits trialed, not ported; new on rebase line | this P1 change | new + adapted | 64-bit ext4 gate evidence, 93 host tests |

`superseded` means the old implementation exists, but the current line's
implementation is the one to keep. Do not replace current modules with the old
copies merely for provenance.

## Desktop consolidation (2026-10-31)

A direct QEMU boot using `-kernel Image -initrd <full Stage1 cpio> -dtb
fb-bochs.dtb` reached systemd, Xorg, the window manager, the desktop session,
and `panel-start` on the current `browser-web` root. The compact
`tools/riscv/debian/desktop_gate.py` now checks those ordered markers and
passes with a real QEMU run. U-Boot and the historical M3/M4 gate stack are not
needed for this check.

As a result, the previously picked scaffolding was removed:

- `qemu_qmp.py`, `qemu_ppm.py` (`961b7ceb7`) — removed;
- `gate_protocol.py`, `gate_runtime.py`, `rootfs_gate.py`,
  `rootfs_gate_backend.py`, `systemd_m2_gate.py` (`6b62ca317`) — removed;
- `desktop_m3_gate.py`, `desktop_m4_gate.py` (`9d597171c`) — removed;
- their wrapper test modules — removed.

Reasons:

- The generic framework is U-Boot/two-boot shaped and much larger than the
  direct-boot desktop check.
- QMP screendump of the bochs display returned a 640x480 VGA image even with
  an explicit display device, while the guest consumes the 1280x1024
  simple-framebuffer. It is not reliable pixel evidence for this boot path.

## P0 desktop evidence (2026-10-31)

The marker-only gate was subsequently found to pass on a dead framebuffer.
Direct boot has no firmware to program the bochs PCI BAR: `info pci` showed
BAR0 at `0x0`, and `pmemsave 0x40000000` returned all `0xffffffff`. The gate
therefore did not prove pixels.

P0 keeps direct boot and extends the same `desktop_gate.py`:

- `make_fb_ram_dtb.sh` moves the simple-framebuffer to the top of QEMU RAM
  (`0xf0000000` for the 2 GiB contract) and shrinks the memory node.
- The browser-web rootfs installs `asterinas-desktop-frame-evidence.service`
  from `desktop_frame_evidence.sh`; it waits for Xorg, settles the desktop,
  finds a visible, geometry-checked Firefox window (`xdotool`, `xwininfo`
  fallback), and emits the Firefox/frame-ready markers.
- The gate passes `ASTERINAS_BROWSER_WEB_BASIC_ONLY=1` and
  `ASTERINAS_WEB_NETWORK_MODE=direct`, captures desktop and Firefox frames
  from guest RAM through the QEMU monitor, validates non-black/colour-rich
  pixels, and requires the Firefox window to change the frame.

The first real pass is
`evidence/2026-10-31-p0-desktop-gate-fb5/`: `status=pass`,
`changed_fraction=0.993874`, and a rendered Firefox New Tab window. A fresh
`browser-web` rootfs was then rebuilt from the current builder and the same
Makefile gate passed on it (`status=pass`, `changed_fraction=0.994758`, title
`Mozilla_Firefox`) under `evidence/2026-10-31-p0-desktop-gate-fresh/`; the
fresh image contains the observer, enabled unit, and `Downloads` directory.
The failed diagnostic runs are preserved under
`2026-10-31-p0-fbprobe-ram/`, `2026-10-31-p0-desktop-gate-fb4/`, and
`2026-10-31-p0-firefox-diag/`. The skill's pitfall table records the dead
BAR, the missing network mode, the `read`/newline bug, and the helper-window
geometry filter.

## P1 broader ext4 coverage (2026-10-31)

The old ext4 feature commits were trialed against the current line before
deciding the P1 scope:

| Source commit | Feature | Result |
|---|---|---|
| `948b2c634` | 64-bit descriptors + `flex_bg` | **Blocked.** The kernel hunks conflict with the current `Dirty`/`sync_metadata` design and expect `stage_metadata`, `StagedGroupMetadata`, and a `desc_size` field from a divergent branch. Current code already validates 64-bit descriptors; `flex_bg` remains unsupported. |
| `772a3de47` | metadata checksums | **Blocked.** Depends on `948b2c634` and journal/checksum-v3 helpers that this line does not have. |
| `511143081` | `FALLOC_FL_PUNCH_HOLE` | **Blocked.** The additive hunks resolved cleanly, but the trial build produced 26 errors against the current extent/indirect APIs (`extent_bytes`, `read_words`, `HasVmReaderWriter`). Picking it would be a new implementation. |
| `14b2d352a` | split oversized journal transactions | **Candidate, not committed.** The earlier "superseded" assessment was wrong: `sync_all` batches many metadata blocks into one transaction, and xfstests generic/006 (4096 files) hits E2BIG. A local multi-descriptor encoder/decoder adaptation made generic/006 pass (70 s), but the 40-test full list still reports a scratch inconsistency at generic/224 and a task-spawn OOM at generic/248. Keep the patch in evidence until dedicated recovery ktests and ENOSPC/task-memory triage exist. |
| `14133b5d3`, `ef12b537a` | EA inodes, tmpfile orphan list | Not trialed; they share the same divergent block-manager/staging base and are outside this P1. |

P1 therefore broadens the **current** driver's evidence instead of forcing an
incompatible feature-branch replay:

- `ext4_gate.py` adds random-data SHA-256 verification, a 200-file directory,
  a 20-level directory tree, hole reads, `fallocate` allocation, and
  `fallocate -z` zero-range verification to the write suite.
- `make_ext4_64bit_image.sh` derives a 64-bit descriptor image from an ext4
  base with `resize2fs -b`, repairs it with `e2fsck -fy`, verifies the
  64-byte descriptor size, and leaves the base image untouched.
- `make test_riscv_debian_ext4_64bit` runs the same gate on that image.
- Host tests cover the new semantics markers, the helper, and the Makefile
  target (93 tests / 10 modules).

Evidence: `evidence/2026-10-31-p1-ext4-64bit-extended/` (semantics plus stage
1 on the generated image) and `evidence/2026-10-31-p1-ext4-64bit-make/` (the
real Makefile target). The pre-broadening baseline is preserved under
`2026-10-31-p1-ext4-64bit/`.

## P2 RISC-V ext4 xfstests (2026-10-31)

The old `8531fae58` ext4 gate was a flat-layout prototype with no successful
RISC-V evidence.  The port keeps the current upstream per-filesystem layout and
adds:

- `test/initramfs/src/conformance/xfstests/ext4/`: compatibility-profile mkfs,
  runtime config, explicit test/scratch mounts, `short.list` (8 cases),
  `full.list` (the old 40-case list), and an inherited block list;
- `tools/qemu_args.sh`: x3/x4 test/scratch images for the RISC-V machine,
  mapped to `/dev/vdd` and `/dev/vde` by the reverse virtio-mmio discovery
  order;
- `test/initramfs/nix/conformance/xfstests.nix`: target-bash repair for the
  three scripts that cross `patchShebangs` had left pointing at the host bash;
- `test/initramfs/src/init`: the `/dev/fd -> /proc/self/fd` alias needed by
  `check`'s process substitution when loading `-E` blocklists;
- kernel ext4 fixes: report `ext4` from `FileSystem::name()` for the ext4 entry
  point, and subtract journal and reserved-GDT blocks in `bsddf` `f_blocks`.

Commits: `66cd86630` (profile/devices/tests), `73241d475` (kernel fixes),
`13fe9f1af` (cross-build/runtime fixes).  Host suite is 100 tests / 11 modules;
RISC-V ktest is 538 passed / 4 known failures (unchanged baseline); the ext4
short list is 8/8.  Evidence:
`/home/ubuntu/.cache/asterinas-sync/evidence/2026-10-31-xfstests-ext4-short/`.

Not yet done: the 40-case `full.list` and the inherited block-list triage; the
`make run_kernel` wrapper was not exercised end-to-end (the validated run used
the same OSDK arguments directly with a prebuilt initramfs).

Full-list triage attempts:

- 4 GiB: page-table allocation panic (`Failed to allocate a page table node:
  NoMemory`) at generic/100.
- 12 GiB: ext4/042 through generic/246 pass (29 tests).  generic/006 first
  exposed the single-descriptor journal limit; a local multi-descriptor
  encoder/decoder adaptation made it pass (70 s), but generic/224 then reported
  `_check_generic_filesystem: filesystem on /dev/vde is inconsistent`, and
  generic/248 panicked at `spawn task failed: NoMemory`.  The candidate patch
  and both logs are under
  `evidence/2026-10-31-xfstests-ext4-short/full-list-journal-candidate.patch`.
  It is not committed: the decoder has no dedicated recovery ktest, and the
  ENOSPC/task-memory failures are independent blockers.  The full list is not
  a gate yet; the 8-case short list remains the validated result.

## Decisions

- **Skip** board/GPU/DRM/Megrez commits from the old tree; not part of this
  QEMU workstream.
- **Skip** the old ext4 implementation as a pick source; the current
  `30cd05424` plus `31acb7c3c` stack is the maintained implementation.
- **Do not** merge old branch history; the old and new trees have different
  paths and contracts.
- **Do not** pick a gate before its closure; clean textual application is not
  a runnable feature.

The branch state after the xfstests short-list work is recorded in
`docs/porting/2026-10-31-merge-route.md`: the validated code tip
`0d552997f` is tagged `pre-main-promotion`, and `rebase/upstream-20261029` is
fast-forwarded to the line tip.  `backup/rebase-20261031` keeps the previous
integration tip.  The line was pushed on the fork: `origin/main-next` is the
fork default, `origin/rebase/upstream-20261029` is the same tip, both tags are
pushed, and `archive/legacy-main-2026-10-31` preserves the old `origin/main`
at `af48751df`.  Old `main` was not deleted or renamed.

The ext4 kernel fixes and the ext4 xfstests profile are **not** standalone
upstream slices: they depend on the downstream ext4 extents/JBD2 feature stack,
which is absent from `upstream/main`.  Only `/proc/self/exe` and the generic
`/dev/fd` / cross-shebang test-infrastructure fixes are independent candidates.

## Next action

The P0 desktop Firefox/framebuffer gate and the P1 broader ext4 matrix are
delivered. The old branch's downstream ext4 xfstests gate (`8531fae58`) is a
separate, still-unported layer; see
`docs/porting/2026-10-31-xfstests-survey.md` for the gap analysis and port
plan. Next candidates, in priority order:

1. RISC-V/ext4 xfstests: the 8-case short list is green; run and triage the
   40-case `full.list`, then settle the inherited block list and the Makefile
   entry point;
2. IPv6 route netlink and route deletion;
3. dsh service/launcher integration;
4. unported ext4 features (metadata checksums, `flex_bg`, EA inodes, tmpfile),
   only after a separate design decision on replacing the current staging
   model.

Every new source-based change still gets a `Picked-from:` or `Ported-from:`
trailer and a row here.
