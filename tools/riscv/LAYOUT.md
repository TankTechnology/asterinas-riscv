# RISC-V tooling layout

Where host-side RISC-V tooling lives, and where new files belong.
`tooling_surface.json` is the enforced part of this page: the top-level
shell entry points and the production file ceilings.

## Entry point

`tools/riscv/rt <command> [args...]` is the single public entry point. It
forwards to the module or script that implements the command, after putting
the repository root and the tooling directories on `PYTHONPATH`, so the same
command works from any directory and from a container. `rt --list` prints the
command set, and `rt <command> --help` shows that command's own arguments.

Implementation lives in the domain packages (`board/`, `qemu/`, `debian/`,
`ltp/`, `megrez/`, `drm/`, `perf/`, `ktest/`, `diagnostics/`).
Only `rt` is a top-level executable; two top-level modules remain for
compatibility: `host_test_runner.py` (the test runner itself) and
`physical_graphics_qemu_gate.py` (a SHA-256-pinned experiment imports it by
its top-level name).

## Directories

| Directory | Owns |
| --- | --- |
| `board/` | Board serial console, portal/recovery helpers, image preparation (the `board` CLI) |
| `megrez/board/` | Megrez session/boot/menu, probes, recovery, RockOS attestation, debug-plan workflow |
| `megrez/desktop/` | Megrez desktop boot/diagnose, Firefox daily-use, physical graphics |
| `megrez/image/` | Debian install/shell, DTB check/patch |
| `megrez/network/` | GMAC contracts/gates, network fixture, proxy bridge |
| `qemu/` | QEMU `virt` + U-Boot runner: profiles, device sets, dtb/artifact checks, QMP, framebuffer capture, the initramfs builder and the prepared-boot script |
| `debian/` | Debian image build, rootfs profiles, and the M1–M9 daily-use/desktop/browser gates |
| `ltp/` | LTP manifests, baselines, suite data, the `ltp-gate.sh` one-command driver, and the guest builders under `ltp/guest/` |
| `drm/` | DRM gates and pixel-oracle helpers |
| `diagnostics/` | One-shot probes and guest-side diagnostics |
| `perf/` | Host-side benchmarks and sampling helpers |
| `ktest/` | Focused kernel-test runners (`kernel_ktest.sh`, `gate_ktests.sh`) |
| `concurrency-models/` | loom crates, TLA+ specs and the Kani proof harness |
| `nsmatrix/`, `pidns/`, `xhci/` | Namespace and USB bring-up |
| `tests/` | The host test suite; every gate above has a contract test here |

## Import rules

- A module that imports another lives in the same directory or relies on
  `PYTHONPATH` listing `tools/riscv`, `tools/riscv/qemu` and the four
  `tools/riscv/megrez/*` directories.
- `PYTHONPATH=tools/riscv:tools/riscv/qemu:tools/riscv/megrez/{board,desktop,image,network}`
  is the shape the `Makefile` and the gate builders use. Add the entry for
  any directory a new target imports from.
- `tools/riscv/tests/__init__.py` installs the same list for package
  imports; `test_drm_firmware_gate.py` installs it for direct discovery
  (`python3 -m unittest discover`), where the package `__init__` does not
  run.

## Top level

Everything that used to sit at the top level as a script now lives in a
domain package and is reached through `rt`. The `megrez_*` modules were
grouped under `megrez/{board,desktop,image,network}/` on 2026-11-05, and the
remaining loose modules moved into their packages on 2026-11-09.

`tooling_surface.json` caps the top-level shell scripts at zero and the
production file counts at their current values; new tooling goes into a
package behind an `rt` subcommand.

## Script status

Scripts with no test reference carry a `# Status:` line in their header and a
row here. The categories are:

- **active** — a current Makefile target or gate invokes it;
- **manual** — run by hand for a documented workflow; no automated caller;
- **legacy** — historical milestone entry point; kept to reproduce a dated
  report, not part of the current workflow.

| Script | Status | Why / replacement |
| --- | --- | --- |
| `ktest/gate_ktests.sh` | active | invoked by the Makefile's Megrez kernel-test target (`rt ktest-gate`) |
| `qemu/eic7700_isolation.sh` | manual | board isolation check (`rt isolation`), referenced by the status page |
| `debian/rootfs/firefox_kernel_static_check.sh` | manual | static compile/lint gate (`rt firefox-static`); also py_compiled by `firefox_fast_check.sh` |
| `board/make_megrez_package.sh` | manual | board image packaging (`rt package`); see `docs/porting/megrez-board-session-commands.md` |
| `board/paced_serial_bridge.py` | manual | serial file-transfer helper (`rt serial-bridge`); see `megrez-board-session-commands.md` |
| `qemu/reboot_sync_gate.py` | manual | QEMU reboot writeback gate (`rt reboot-sync`); documented in the RISC-V README |
| `qemu/verify_megrez_sim.sh` | manual | boot-chain simulation check (`rt verify-sim`); see `drm/verify_megrez_drm_sim.sh` |
| `megrez/board/megrez_recover.py` | manual | board recovery entry; see `docs/porting/board-addresses.md` |
| `megrez/board/megrez_control_chain.py` | manual | RockOS control-chain protocol; no live caller or document reference — confirm before removal |
| `megrez/desktop/megrez_desktop_baseline.py` | manual | one-shot desktop performance baseline |
| `diagnostics/syscall_ipc_bench_guest.py` | manual | guest-side syscall/IPC benchmark pushed by `megrez_desktop_baseline.py` |

Counts: 1 active, 10 manual, 0 legacy (11 scripts).

The NixOS milestone entry points (`fm3-gate.sh`, `fm4-gate.sh`, `pm1-gate.sh`)
and the `nixos/` milestone tree were removed on 2026-11-09; see
`docs/porting/2026-11-09-tooling-cleanup.md` and the archive ref it names.

`diagnostics/firefox_checkpoint_baseline.py` is not marked: the tested
`firefox_dmesg_experiment.py` pins its SHA-256, so its content is part of
that experiment and cannot carry an editable header.

## Historical documents

Evidence and plan documents under `docs/porting`, `docs/performance` and
`docs/superpowers` record what was run on a given date. They are not
rewritten when a path moves: a command in a dated document may name
`tools/riscv/qemu_uboot_booti.py` or `tools/riscv/megrez_debug.py`, while
the live files are `tools/riscv/qemu/qemu_uboot_booti.py` and
`tools/riscv/megrez/board/megrez_debug.py`. Use this page (or the code) as
the current answer. Dead files removed from this tree are listed in
[`docs/porting/2026-11-09-tooling-cleanup.md`](../../docs/porting/2026-11-09-tooling-cleanup.md);
the page also names the archive ref that still contains them.

## Verifying a change

```bash
make test_riscv_host              # unit tier, a few seconds (default)
make test_riscv_host_integration  # process-spawning tier, also nightly
make test_riscv_host_all          # both tiers
./tools/format_all.sh --check     # rustfmt
```

The default `make test_riscv_host` runs the **unit tier only**. Modules listed
under `integration` in `tests/tiers.json` are skipped by it — including the
whole builder/rootfs group (`test_debian_rootfs`, 208 tests). Use
`make test_riscv_host_all` (or `make test_riscv_host_integration`) after
touching `build_rootfs.sh` or anything under `debian/rootfs/`; `rt test --list`
without `--tier all` is a subset and does not show them.

System validation is moving to the subsystem catalog:

```bash
tools/riscv/rt selftest --list
tools/riscv/rt selftest console net --dry-run
```

Debian application compatibility has its own matrix:

```bash
tools/riscv/rt apps --list
tools/riscv/rt apps --board --install jq mousepad
```

The matrix lives in `apps/manifest.py`; the guest probe is
`apps/guest/probe.sh`. `--install` runs apt, otherwise the guest must already
have the packages.

A DeepSeek-driven board agent can operate the board through the serial bridge.
**Naming note:** this local `dsh` is not the official DeepSeek Harness
(`deepseek-ai/deepseek-harness`, `@deepseek-ai/dsh`), which is a TypeScript /
Cordis plugin runtime documented in
[`docs/porting/2026-11-10-node24-jit-and-dsh.md`](../../docs/porting/2026-11-10-node24-jit-and-dsh.md).
The shared name is a local collision; this tool has no sandbox or permission
layer.

```bash
tools/riscv/dsh --dry-run
tools/riscv/dsh "运行 uname -a 并报告内核版本"
```

`dsh` reads the API key from the environment and exposes only board tools
(run, read log, verified push, opt-in reboot); see
[`docs/porting/2026-11-09-deepseek-harness.md`](../../docs/porting/2026-11-09-deepseek-harness.md).

Until the guest bundle lands, integration gates run through the transitional
scenario registry, for example
`tools/riscv/rt gate browser-web` or `tools/riscv/rt gate daily-use`; the
Makefile targets `test_riscv_*` are the supported spellings. The suite count,
tiers and expected skips live in `tests/expected_host_tests.json` and
`tests/tiers.json`; see `MAINTAINABILITY.md` and
[`docs/porting/2026-11-09-test-tiers.md`](../../docs/porting/2026-11-09-test-tiers.md)
for the rules on updating them.
