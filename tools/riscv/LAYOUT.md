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
| `ltp/` | LTP manifests, baselines, and suite data |
| `drm/`, `xorg/`, `wayland/`, `xfce/`, `lvgl/`, `game/` | Graphics bring-up, per milestone |
| `diagnostics/` | One-shot probes and guest-side diagnostics |
| `perf/` | Host-side benchmarks and sampling helpers |
| `compat/` | Developer-workflow and API acceptance checks |
| `concurrency-models/` | loom crates, TLA+ specs and the Kani proof harness |
| `systemd/`, `nixos/` | Runtime and OS-image experiments |
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

## Historical documents

Evidence and plan documents under `docs/porting`, `docs/performance` and
`docs/superpowers` record what was run on a given date. They are not
rewritten when a path moves: a command in a dated document may name
`tools/riscv/qemu_uboot_booti.py` or `tools/riscv/megrez_debug.py`, while
the live files are `tools/riscv/qemu/qemu_uboot_booti.py` and
`tools/riscv/megrez/board/megrez_debug.py`. Use this page (or the code) as
the current answer.

## Verifying a change

```bash
make test_riscv_host            # the contract suite
./tools/format_all.sh --check   # rustfmt
```

The suite count and expected skips live in `tests/expected_host_tests.json`;
see `MAINTAINABILITY.md` for the rules on updating them.
