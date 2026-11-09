# RISC-V tooling layout

Where host-side RISC-V tooling lives, and where new files belong.
`tooling_surface.json` is the enforced part of this page: the top-level
shell entry points and the production file ceilings.

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
| `ltp/` | LTP manifests, baselines, suite data, and the guest builders under `ltp/guest/` |
| `drm/`, `xorg/`, `wayland/` | Graphics bring-up, per milestone |
| `diagnostics/` | One-shot probes and guest-side diagnostics |
| `perf/` | Host-side benchmarks and sampling helpers |
| `concurrency-models/` | loom crates, TLA+ specs and the Kani proof harness |
| `systemd/` | systemd image experiments and the boot driver used by the xorg render gates |
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

The `megrez_*` modules were grouped under `megrez/{board,desktop,image,network}/`
on 2026-11-05; only `megrez_contract.py` (the shared contract loader imported
by name from several places) remains at the top level. What is left there now
is the entry-point and runner set: `host_test_runner.py`, `ltp_gate.py`,
`reboot_sync_gate.py`, `firefox_debug_tool.py`, `firefox_fast_check.sh`,
`lmbench_*`, `hdmi_pixel_oracle.py`, `paced_serial_bridge.py`, the
`drm/xorg/...` gate scripts and a few manual board runners.

Only shell scripts listed in `tooling_surface.json` may sit at the top level.

## Script status

Scripts with no test reference carry a `# Status:` line in their header and a
row here. The categories are:

- **active** — a current Makefile target or gate invokes it;
- **manual** — run by hand for a documented workflow; no automated caller;
- **legacy** — historical milestone entry point; kept to reproduce a dated
  report, not part of the current workflow.

| Script | Status | Why / replacement |
| --- | --- | --- |
| `gate_ktests.sh` | active | invoked by the Makefile's Megrez kernel-test target |
| `demo-all.sh` | manual | one-command demo; see `README-DEMO.md` |
| `eic7700_isolation.sh` | manual | board isolation check referenced by the status page |
| `firefox_kernel_static_check.sh` | manual | static compile/lint gate; also py_compiled by `firefox_fast_check.sh` |
| `make_megrez_package.sh` | manual | board image packaging; see `docs/porting/megrez-board-session-commands.md` |
| `paced_serial_bridge.py` | manual | serial file-transfer helper; see `megrez-board-session-commands.md` |
| `reboot_sync_gate.py` | manual | QEMU reboot writeback gate; documented in the RISC-V README |
| `verify_megrez_sim.sh` | manual | boot-chain simulation check; see `drm/verify_megrez_drm_sim.sh` |
| `megrez/board/megrez_recover.py` | manual | board recovery entry; see `docs/porting/board-addresses.md` |
| `megrez/board/megrez_control_chain.py` | manual | RockOS control-chain protocol; no live caller or document reference — confirm before removal |
| `megrez/desktop/megrez_desktop_baseline.py` | manual | one-shot desktop performance baseline |
| `diagnostics/dhcp_probe.py` | manual | guest-side DHCP probe |
| `diagnostics/fb_downsample_guest.py` | manual | guest-side framebuffer downsample probe |
| `diagnostics/gdk_pixbuf_probe.py` | manual | guest-side gdk-pixbuf/wallpaper probe |
| `diagnostics/syscall_ipc_bench_guest.py` | manual | guest-side syscall/IPC benchmark pushed by `megrez_desktop_baseline.py` |

Counts: 1 active, 14 manual, 0 legacy (15 scripts).

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
make test_riscv_host            # the contract suite
./tools/format_all.sh --check   # rustfmt
```

The suite count and expected skips live in `tests/expected_host_tests.json`;
see `MAINTAINABILITY.md` for the rules on updating them.
