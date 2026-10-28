# RISC-V tooling maintainability baseline

This page records the local, reproducible baseline for the
`tools/riscv` host test suite. It is intentionally small; detailed
refactoring work is tracked separately.

## Baseline

- Current baseline after retiring the `browser-m5` and `desktop-network` image
  profiles.
- The executable host-test baseline is
  `tools/riscv/tests/expected_host_tests.json`; it records the exact test IDs
  and expected skips, so this page does not duplicate volatile file or test
  counts.
- Kernel ktest baseline (`make ktest TARGET_ARCH=riscv64 -j32`):
  **777 passed / 0 failed / 32 crates / 257 s**
- Canonical command:

```bash
make test_riscv_host HOST_TEST_JOBS=32
```

The runner discovers every `tools/riscv/tests/**/test_*.py` module, runs it in
an isolated subprocess, records per-module logs under `target/host-tests/`,
and checks the result against `tools/riscv/tests/expected_host_tests.json`.
A missing baseline test ID, an unexpected skip reason, a duplicate test ID, a
timeout, or an import failure is an error.

List the exact collected IDs without running the tests:

```bash
make test_riscv_host_list
```

The canonical unittest command remains available:

```bash
PYTHONPATH="$PWD" python3 -W error::ResourceWarning \
  -m unittest discover -s tools/riscv/tests -t . -p 'test_*.py' -v
```

## Expected skips

The baseline allowlist in `tests/expected_host_tests.json` names these
environment-gated groups:

- missing pinned Firefox archive / cached Node;
- the local experimental Firefox runner;
- the RISC-V cross compiler outside the development container;
- host DRM uapi headers that predate `VIRTGPU_CONTEXT_INIT`.

Adding any other skip requires an explicit baseline update in the same change.

## Phase 0 fixes found while measuring

The baseline run exposed three pre-existing host-test issues, fixed before the
baseline was recorded:

- `test_drm_uapi_contract`: skip explicitly when the host headers lack the
  virtgpu context-init uapi instead of failing to compile.
- `test_drm_virgl_param_gate`: wait for the lifecycle `READY` marker rather
  than a fixed number of lines, which was racy under parallel load.
- `test_physical_graphics_qemu_gate`: accept the parameterized
  `bochs-display,xres=...` device spelling.

## Phase 2 completion record

The current product path is the `browser-web` image in
`docs/porting/board-image-current.md`. The cleanup removed the historical image
families and their test-only scaffolding:

- Active rootfs profiles are `minimal`, `systemd-ext4`, `browser-web`, and the
  paused `desktop-drm`. The old M1–M9 profile identities are gone.
- `browser-m5` assets, offline video/probe scripts, QEMU gate, and profile-only
  tests are retired. `browser-web` owns its package set directly, and signed
  source tests use the browser-web schema.
- `desktop-m5-network` is no longer an image profile. Its network evidence
  service and QEMU network gate remain installed by `browser-web`.
- M6/M7 guest services, gates, and tests are archived under
  `tools/riscv/archive/milestones/`. GMAC exposes only the current `network`,
  `desktop`, and `firefox` targets; the debug contract uses browser-web
  readiness markers.
- Current naming is functional: `desktop_network_*` for the network service,
  `browser_marionette_transport` for the shared transport, and
  `desktop-browser-session` for the browser desktop. The active
  code/Makefile/test surface has no M5 naming.
- File-count limits were removed. The executable baseline in
  `tests/expected_host_tests.json` remains the local test gate; no production
  or module count is duplicated in documentation.

Evidence under `docs/porting/evidence/` and historical reports remain intact.
The archived reports are indexed in `tools/riscv/archive/README.md`; archived
implementation code was removed from git after a local backup.

## Minimal keep set

The current product path does not need every historical acceptance script.
Keep these entrypoint families:

- `build_rootfs.sh` and `build_stage1.sh` with the profile/contract/gate
  libraries they import.
- `browser_web_qemu_gate.py`, the browser-web contract/Marionette/evidence
  modules, and the guest scripts installed by the rootfs builder.
- `megrez_debian_install.py`, `megrez_installer.py`, the board-session,
  persistent-shell, boot-menu, xmodem, clock, DNS, and safe-reboot modules.
- `physical_graphics_gate.py`, `physical_graphics_qemu_gate.py`,
  `debug_console_qemu_gate.py`, and the current QEMU/U-Boot gate modules.
- `host_test_runner.py`, the test support package, and the LTP/perf core.
- The paused `drm/` module set remains frozen until GPU work resumes.

One-off Firefox/dmesg diagnostics, standalone probe tools, SDHCI probes,
thread/video samplers, and QEMU GDB samplers are archived under
`tools/riscv/archive/`. An acceptance or debug script is no longer kept merely
because a historical test or report cited it; it must be reachable from one of
the entrypoint families above.

## Remaining optional structure work

The remaining maintenance cost is production-file size, not obsolete product
surface or test monoliths. All `tools/riscv/tests` modules are now below the
2,000-line guard; the remaining allowlist entries are production files:

- `build_rootfs.sh`,
- `browser_web_marionette_gate.py`,
- `megrez_desktop.py`,
- `megrez_physical_graphics.py`,
- `megrez_probe.py`.

Split these only when their behavior changes; `build_rootfs.sh` should be the
last and highest-risk split because it owns the staged image build. The paused
`desktop-drm` surface stays untouched while GPU work is paused. Archived code
is not imported by active code, tests, Makefile targets, or the documented
product path.

## Next boundaries

The maintainability work proceeds in contract consolidation, test/tool splits,
and dead-code/documentation cleanup. The rule for every change is:

1. add the characterization/contract test first;
2. keep behavior unchanged for refactors;
3. run `make test_riscv_host` and the focused target;
4. update `expected_host_tests.json` only for intentional test additions or
   removals, with the reason in the same change.

Kernel changes stay opportunistic and require `make ktest TARGET_ARCH=riscv64`
with no new failures against the recorded baseline.
