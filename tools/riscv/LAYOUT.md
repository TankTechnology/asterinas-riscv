# RISC-V tooling layout

Where host-side RISC-V tooling lives, and where new files belong.
`tooling_surface.json` is the enforced part of this page: the top-level
shell entry points and the production file ceilings.

## Directories

| Directory | Owns |
| --- | --- |
| `board/` | Megrez serial console, portal/recovery helpers, board image preparation |
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
  `PYTHONPATH` listing both `tools/riscv` and `tools/riscv/qemu`.
- `PYTHONPATH=tools/riscv:tools/riscv/qemu` is the shape the `Makefile`
  and the gate builders use. Add both entries when a new target imports a
  module from either place.
- Tests that add `tools/riscv` to `sys.path` must also add
  `tools/riscv/qemu` before importing a `qemu_*` module.

## Top level

The remaining top-level modules are the `megrez_*` cluster plus a handful of
runners (`host_test_runner.py`, `ltp_gate.py`, `reboot_sync_gate.py`,
`lmbench_*`, `firefox_*`, `hdmi_pixel_oracle.py`, …). A follow-up move can
group `megrez_*` under `megrez/`; until then they stay at the top level
because their paths are asserted by the existing contract tests and by the
dated evidence under `docs/` that quotes the commands that were run.

Only shell scripts listed in `tooling_surface.json` may sit at the top level.

## Historical documents

Evidence and plan documents under `docs/porting`, `docs/performance` and
`docs/superpowers` record what was run on a given date. They are not
rewritten when a path moves: a command in a dated document may name
`tools/riscv/qemu_uboot_booti.py`, while the live file is
`tools/riscv/qemu/qemu_uboot_booti.py`. Use this page (or the code) as the
current answer.

## Verifying a change

```bash
make test_riscv_host            # the contract suite
./tools/format_all.sh --check   # rustfmt
```

The suite count and expected skips live in `tests/expected_host_tests.json`;
see `MAINTAINABILITY.md` for the rules on updating them.
