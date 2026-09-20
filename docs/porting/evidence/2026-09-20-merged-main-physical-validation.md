# Merged-main QEMU and physical validation, 2026-09-20

## Scope

This record covers the integration of `codex/firefox-daily-use-perf` into the
downstream `main` line and the re-validation of that merged tree on QEMU and on
the physical board. It is the evidence behind the current `docs/porting/README.md`
boundary.

It is **not** a new performance result. The Firefox daily-use performance
baseline and the waking-task A/B remain the records they already were; this page
only establishes that the merged tree reproduces their deployment identity and
still boots.

## Source identity

| Item | Value |
| --- | --- |
| Merge commit | `bb76f8d63` (parents `d67fe20ec`, `1af7bc2a3`) |
| Validated commit | `d38a0fb28` (merge plus the formatting commit below) |
| Upstream base at merge | `origin/main` `4612e4f35` |

The merge integrates 52 commits. The three files changed on both sides
(`tools/riscv/README.md`, `tools/riscv/megrez_board_session.py`, and its test)
merged without conflict and were reviewed for meaning, not just for text: the
`120`-second default that `main` lowered applies to
`test_riscv_megrez_debug_board`, while the desktop-boot path owns a separate
300-second budget in `tools/riscv/megrez_desktop_boot.py` and does not inherit
it.

## Deployment identity after the merge

| Artifact | SHA-256 | Note |
| --- | --- | --- |
| Kernel (Sv39, SMP=4, `RELEASE=1`) | `8c470f0968a8983b4ad6b3dd313d89d45c4f89d7b39ff6ebaaae566681ef42ed` | `kernel/` and `ostd/` sources are identical to variant B (`53c4601a6`); the bytes differ only because the two were built from different worktree paths and the repository does not remap build paths. |
| Stage1 | `9bcf5f7b2f2755c98fa3c4461314a876b5a2c3551c3746fa3a0a614b08fceafb` | Byte-identical to `stage1-crossarch-a`/`-b`, the Stage1 the physical A/B runs used, and reproducible by two independent builds of the merged tree. |
| Megrez DTB | `465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba` | Unchanged. |
| Board root | `80b1118790b70cd88c3133d8fbc5beab88ae22c2c56443c058a88f4e900fb99c` | The root already installed on eMMC partition 2. It was **not** rebuilt; see the boundary note below. |

The Stage1 result matters for the earlier evidence: the physical A/B records bind
`9bcf5f7b`, so their Stage1 binding still describes the merged line.

## QEMU result

`make test_riscv_physical_graphics_qemu_gate`, four harts, with the merged
kernel, the rebuilt Stage1, and a development-overlay root:

- `passed = true`, `reason = pass`, `physical = false`, three interaction cycles.
- Evidence: `target/firefox-daily-use-physical/qemu-qualification-merged/`
  (`result.json` plus three PNG/PPM cycle captures and the serial transcript).
- Recorded inputs: kernel `8c470f09`, Stage1 `9bcf5f7b`, root `b97ea1a2`,
  U-Boot `ff9abcc4`, DTB `586d1136`.

Reading this directory requires the container or root: the gate creates its
output directory root-owned mode `0700` from inside the development container,
so an unprivileged host-side listing cannot read it. An unreadable directory is
not an empty one.

## Physical result

Two bounded board experiments, both taken after publishing the merged
generation `70dd75d03b9b7ef1eebb5c96b7b23aaad2d8d5761c059b2d7b1ad50a2010287a`
to RockOS partition 3.

| Experiment | Result | Evidence |
| --- | --- | --- |
| `make run_riscv_megrez_desktop` | `status = pass`, `reason = desktop-ready`, `elapsed_seconds = 95.73`; desktop-ready at 61.83 s, debug console, `firefox_pid = 172` (uid 1000), one visible window, X11 socket, `watchdog = 0` | `target/megrez-desktop-boot/start-20260920T055854Z/` |
| `python3 -m tools.riscv.megrez_firefox_daily_use` | `passed = true`, `qualified = true`, `recovered = true`, terminal `outcome = pass`, `gate_status = 0`, `upload_status = 0`, all seven functional groups pass | `target/firefox-daily-use-physical/run-m1/` |

The daily-use run's own deployment block records `commit = d38a0fb28`, which is
the validated tree, together with the artifact digests above. It is a **smoke
validation of the merged line**, not a re-qualification: the daily-use baseline
is defined as a closed three-run experiment, and one qualified run does not
renew it.

## What this record does not claim

- **The root filesystem is not a merged-tree build.** `main` changed
  `tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh`, which
  `build_rootfs.sh` installs as `/usr/lib/asterinas/desktop-m5-network-evidence`
  and which participates in the browser-web runtime digest. The board runs the
  previously installed root, and the QEMU gate ran against a development
  overlay derived from the frozen `browser-web` base rather than a full rebuild.
  `make run_riscv_megrez_desktop` does not rewrite the root by design, and this
  validation did not reinstall one.
- **That script is not exercised by these experiments.**
  `physical_external_services_quiesce.sh` masks
  `asterinas-desktop-m5-network.service`, and the desktop-boot plan masks it
  again on the kernel command line. Validating it belongs to the M5 network
  gate, not to this one.
- No performance claim is made or renewed here. The waking-task A/B keeps its
  narrow admission: `contextSwitchTotalMs` and both scroll metrics improve with
  no overlap, six of nine primary metrics overlap, and `contextSwitchTotalMs`
  remains over its diagnostic threshold, so the next kernel-side step is one
  bounded attribution observation.
- Kernel-test counts are not browser performance. The suite reports 242 passed
  and 2 failed in `aster_kernel`, and 18 passed and 2 failed in `xarray`; the
  identical failure set occurs at `55ee5c64e`, so those four pre-date this line.

## Repository checks

`make check TARGET_ARCH=riscv64` was run for the first time on this line. It
required formatting changes to two Rust and thirteen C files plus one Nix file,
which are applied. Do not use the repository-level `make format` for this: its
first step runs `sed -i 's/ *$//'` over every git-tracked non-patch file and
rewrites binary tracked files such as
`tools/riscv/xfce/shots/xfce-desktop-full.png`; the corresponding check step
skips binaries.

Three check failures remain and are **pre-existing on `origin/main`**, not
introduced by this merge: `clippy` reports `is_unique` and `as_non_null_ptr` as
never used when `ostd` is built for `riscv64imac` (both are called from
`ostd/src/mm/dma`, which that configuration does not compile), and `typos`
flags "Synopsys" in `kernel/comps/dwmac/src/lib.rs`, which is the vendor name.
The RISC-V CI kernel-test job will also be red for the four failures above; that
is the pre-existing condition, not a regression.
