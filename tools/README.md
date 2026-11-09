# `tools/` — development, test and board tooling

This tree contains everything that is **not shipped as part of the kernel**: build helpers,
board bring-up scripts, QEMU runners, benchmark harnesses and verification drivers.
Nothing here is required to build `osdk` itself; it exists so that a contributor can go from
"checkout" to "booted on hardware" without re-deriving commands.

> Convention: every script starts with an SPDX header (MPL-2.0) and resolves the repository
> root by sourcing [`lib/shell.sh`](lib/README.md) and calling `repo_root()` instead of
> hard-coding paths.

Where to start:

- RISC-V/Megrez tooling map: [`riscv/LAYOUT.md`](riscv/LAYOUT.md).
- Board network settings (never committed): [`docs/porting/board-addresses.md`](../docs/porting/board-addresses.md).
- Staged-secret check before a commit: [`check_secrets.sh`](check_secrets.sh).

## Top-level scripts

| Script | Purpose |
| --- | --- |
| `format_all.sh` | Run `rustfmt`/formatting checks across the workspace. |
| `clippy_check.sh` | Run Clippy with the repository's lint configuration. |
| `ktest.sh` | Run the kernel test suite (`make ktest`) and print an aggregate result. |
| `qemu_args.sh` | Generate the QEMU argument list consumed by OSDK. |
| `atomic_wget.sh` | Download to a temporary file and atomically replace the destination. |
| `bump_version.sh` | Bump workspace version metadata. |
| `print_workspace_members.sh` | Print the workspace member list (used by CI and scripts). |
| `sctrace.sh` | syscall trace helper. |
| `check_secrets.sh` | Reject staged credentials and real public IPv4 addresses (`xaj_vibenote.md` §7). |

## Subdirectories

| Directory | Contents |
| --- | --- |
| `lib/` | `shell.sh`: `repo_root()`, `die()`, `require_cmd()`, `log_step()` shared by every tooling script. |
| `riscv/` | **The bulk of the tree.** RISC-V/Megrez board bring-up, Debian image, QEMU runners, perf and DRM gates (159 shell + 350 Python files). |
| `github_workflows/` | Helpers invoked from `.github/workflows`. |
| `verification/` | Focused verification drivers (scheduler hand-off, fair yield, signal/job control, local migration…). |
| `docker/` | Container assets, including the RISC-V rootfs builders. |
| `nixos/` | Nix-based image and toolchain experiments. |
| `benchmarks/` | Micro-benchmark drivers. |
| `net/` | Network setup helpers. |
| `usb-hid/` | USB HID device helpers and tests. |
| `vnc-capture/` | VNC screen capture tool (Rust; build products land in `target/`). |
| `x86/` | x86-specific helpers. |
| `debian/`, `sctrace/` | Legacy layout kept for compatibility. |

## `tools/riscv/` at a glance

| Directory | sh | py | Notes |
| --- | ---: | ---: | --- |
| `debian/` | 44 | 60 | Debian image build, daily-use image, apt/fixture checks. |
| `nixos/` | 29 | 25 | NixOS image experiments. |
| `perf/` | 21 | 3 | Benchmark harnesses and syscall/perf baselines. |
| `systemd/` | 16 | 8 | systemd component builders (dbus, agetty, libcap…). |
| `drm/` | 8 | 16 | DRM gates: firmware, GEM, cursor, page-flip. |
| `tests/` | 0 | 128 | Python test drivers for the RISC-V paths. |
| `qemu/` | 3 | 19 | QEMU runners, GDB probes, live samplers. |
| `diagnostics/` | 2 | 14 | klog/X11/bench diagnostics. |
| `wayland/` | 3 | 0 | Wayland bring-up. |
| `concurrency-models/` | 1 | 1 | Rust/TLA+ concurrency models; build output is gitignored (1.4 GB on disk). |
| `board/` | 0 | 6 | Board-side helpers (serial, flashing). |
| `xhci/` | 1 | 1 | xHCI input gates. |
| `legacy/` | 7 | 5 | Historical demos and experiments (game, lvgl, xfce, compat); not in the current workflow. |
| `ltp/`, `nsmatrix/`, `pidns/`, `xorg/` | — | — | Smaller or one-shot drivers. |

13 shell and 61 Python scripts also live directly in `tools/riscv/`; they predate the
subdirectory split and are kept at that level so existing command lines keep working.

## Ground rules

- **Build output never gets committed.** `target/`, `__pycache__/`, `*.pyc` and model-checker
  `states/` are covered by `.gitignore`; keep it that way when adding a new tool.
- **Board scripts must be rerunnable.** They assume the board may be in an unknown state and
  print what they verified instead of assuming success.
- **One directory per concern.** Add new work under the closest existing subdirectory; only
  put a script at `tools/` or `tools/riscv/` top level if it is a general entry point.
- **Document the entry point.** If a subdirectory has more than one script, add a short
  `README.md` next to it (currently only `concurrency-models/`, `ltp/`, `perf/`, `wayland/`
  and `xhci/` have one).
