# RISC-V tooling maintainability baseline

This page records the local, reproducible baseline for the
`tools/riscv` host test suite. It is intentionally small; detailed
refactoring work is tracked separately.

## Baseline

- Source: `origin/main` at `3ceeccd04` before the maintainability branch.
- Host modules: **123**
- Host tests: **2,463**
- Expected environment skips: **13**
- Production Python files: **204**
- Production shell files: **148**
- Top-level shell entrypoints: **15**
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

## Phase 2 necessity audit

This is a decision record for the cleanup, not a deletion manifest. It answers
the first Phase 2 question: which surface is still reachable from the current
product path, and which surface is only kept alive by its own tests or by
historic documentation. No file is removed by this section.

The current product authority is the 2026-10-28 single-image board record in
`docs/porting/board-image-current.md`: one `browser-web` rootfs on partition 2,
one daily boot entry, and the existing board-install/persistent-shell path. A
directory-wide reference count is not enough: the old research trees contain
many internal cross-references even when nothing current enters them.

### Active surface

| Surface | Decision | Why |
|---|---|---|
| `browser-web` profile | Keep | Current daily image, QEMU browser gate, physical graphics and Firefox daily-use gates. |
| `minimal-m1` profile | Keep | Default builder profile; persistent Debian shell and board-install contract. |
| `systemd-ext4-m3` profile | Keep for now | The only remaining ext4/systemd qualifier; can absorb the old `systemd-m2` boot check. |
| `desktop-drm` profile and DRM gates | Paused | GPU/virtio-gpu work is frozen; do not delete or promote it in this cleanup. |
| M1 two-boot rootfs gate | Keep | Proves board-install persistence independently of the desktop image. |
| browser-web QEMU gate | Keep | Primary QEMU acceptance for the current image. |
| M5 network `network` target | Keep | It already accepts schema-7 `browser-web` artifacts; only the old M5 `browser` target is obsolete. |
| debug-console and physical-graphics gates | Keep | They consume the current browser-web/board artifacts. |
| physical Firefox daily-use gate | Keep | This, not the old QEMU `daily_use_gate`, is the required daily-use coverage. |
| board install, persistent shell, boot menu, boot stability | Keep | The current board deployment path still uses the menu/manifest/shell contracts; do not archive them with the milestone gates. |
| `tools/riscv/perf/` contract core, native/smoke LMBench, `make_megrez_package.sh`, `verify_megrez_sim.sh` | Keep | Recent benchmark/LTP tooling and the documented board preflight/package workflow; they are not obsolete just because they lack a Makefile target. |
| host runner and baseline manifest | Keep | The local gate for every cleanup change. |

### Consolidation and archive candidates

Zero textual references is not automatically safe to delete: some files are
manual harness leaves or are referenced by a dated evidence page. The first
deletion batch is intentionally small.

| Candidate | Decision | Dependency that must move first |
|---|---|---|
| `systemd-m2` profile | Consolidate | Retarget its systemd reboot/power coverage to `systemd-ext4-m3` before removing the profile. |
| `desktop-m3`, `desktop-m4` profiles and gate entrypoints | Archive | Keep the shared `desktop_m3_gate.py` helpers and `DESKTOP_M4_MILESTONES`; current physical/debug gates import them. |
| `desktop-m4-firefox` and `test_riscv_debian_daily_use_gate` | Archive/delete | The Makefile target is broken: it passes empty `--boot-timeout ""` and `--command-timeout ""` to a positive-float parser (`rootfs_gate.py:463-464`). The profile was superseded by `browser-web` on the same day. |
| `desktop-m5-network` profile, M5 `browser` target | Consolidate | Keep the M5 network service/evidence layer installed by browser-web; only the profile identity and its browser entrypoint are historical. |
| `browser-m5` profile and QEMU gate | Archive | First inline the package set into `browser-web` (`profiles.py:426-431`); keep `browser_m5_marionette_gate.py`, which is current shared Marionette code. |
| `desktop-m9-software` profile and M9 gate | Archive/consolidate | `vim` is already in browser-web; only the ffmpeg smoke has no browser-web equivalent. |
| M6, M7, M8 gates/services | Archive | NetSurf milestone evidence, installed only for the old M5/M9 profiles and superseded by the Firefox browser-web gate. |
| `tools/riscv/nixos/` except `ltp/` and `build_busybox.sh` | Archive | `ltp_gate.py:631,676,799` hardcodes `nixos/ltp/build_ltp.sh`; `nixos/ltp/build_ltp.sh` needs `nixos/build_busybox.sh`. The rest is the superseded NixOS track. |
| `tools/riscv/systemd/` code, `demo-all.sh`, root `README-DEMO.md` | Delete candidate | A self-contained systemd+NixOS demo chain. Remove the `demo-all.sh` entry from `tooling_surface.json` atomically; archive the reports. |
| `tools/riscv/xorg/` except `gen_tls_certs.py` and `tls_cert_server.py` | Archive | Those two helpers are used by the current M5 network QEMU gate (`desktop_m5_qemu_gate.py:121-122`). |
| `tools/riscv/xfce/` | Archive | No current code reference, but `docs/porting/evidence/2026-09-20-merged-main-physical-validation.md:107` cites its screenshot; keep the evidence link valid or update it when moving. |
| `tools/riscv/wayland/`, `tools/riscv/lvgl/`, `tools/riscv/game/`, `qemu_desktop_boot.py` | Delete candidate | Wayland has only `test_wayland_demo.py`; LVGL is documented only in old QEMU/LVGL sections; `game/snake.c` has zero references and its builder was already removed. |
| `tools/riscv/nsmatrix/`, `tools/riscv/pidns/` and their KE reports | Archive | Historical kernel-namespace results; keep them together as archived evidence, not current tests. |
| `tools/riscv/perf/` GPU/desktop-performance leaves | Paused | `drm-gbm-probe.c`, `boot-bench.c`, `ab-redundant-units.sh`, `sample-qemu-io.sh`, `toggle-desktop-session.sh`, `phase-timing.sh` and `toggle-redundant-units.sh` are DRM/GPU-adjacent. Do not delete while GPU work is paused. |
| `tools/riscv/perf/debian/` unlinked case wrappers | Consolidate after owner review | Added by the recent #184 benchmark contract but not linked from README/Makefile; do not treat as dead solely because grep finds no caller. |
| `build_megrez_sdhci_probe.sh` + `megrez_sdhci_gate.py` | Consolidate | Test/doc-only SDHCI chain (12 tests); retire together with its README section and test. |
| `dual_host_qemu_probe.py` | Consolidate | Reachable only through its Makefile target and 25 baseline tests; retire only if the dual-host probe is no longer needed. |
| `eic7700_isolation.sh` | Archive | Historical status-doc/spec references only; update those links before removal. |

The old reports and logs under `tools/riscv/` are historical evidence, not
active code. Move them to an archive index rather than leaving them beside the
current tools. Two report citations need preserving during the move:
`docs/porting/snd-device-model.md:14` cites `nixos/POLISH-M5-report.md`, and the
2026-09-20 evidence page cites the XFCE screenshot.

### Test-baseline impact

The following modules are historical-only or almost entirely historical. They
should be removed with their feature, not silently skipped:

| Module | Tests |
|---|---:|
| `test_debian_daily_use_gate.py` | 22 |
| `test_debian_m6_browser.py` | 18 |
| `test_debian_m7_baidu.py` | 11 |
| `test_debian_m9_software.py` | 8 |
| `test_debian_browser_m5.py` + `test_debian_browser_m5_qemu_gate.py` | 4 + 6 |
| `test_debian_m8_browser_quality.py` | 5 |
| `test_debian_desktop_m4_framebuffer_probe.py` + `_rw.py` | 6 + 6 |
| `test_wayland_demo.py` | 1 |
| `test_megrez_sdhci_gate.py` | 12 |
| `test_dual_host_qemu_probe.py` | 25 |
| `test_nixos_track_audit.py` | 30 |
| `test_nixos_m7_assets.py` | 6 |

`test_debian_rootfs.py` contains roughly 30 historical profile/gate tests among
its 206; the rest is current gate/runtime/stage1 coverage and must stay.
`test_debian_signed_sources.py` currently uses browser-m5 as a fixture; retarget
it to browser-web instead of deleting its 27 tests. The 36
`test_debian_browser_m5_runtime_gate.py` tests cover the current shared
Marionette library and stay. The 37 DRM tests stay with the paused profile.
The aggregate `test_riscv_debian_rootfs_unit` target still runs the M6/M7/M8/M9
modules and must be trimmed together with those gates.

### Documentation drift

`docs/porting/README.md` still records the 2026-09-20 merged-main boundary and
the old performance objective, and it does not link
`docs/porting/board-image-current.md`, which is the current 2026-10-28
single-image authority. `tools/riscv/README.md` is 1,748 lines across 34
sections and mixes current commands with historical boot-menu, LMBench, probe,
SDHCI and LVGL material. The cleanup must leave one current command page and an
indexed archive; it must not delete the frozen evidence under
`docs/porting/evidence/` (818 files, about 28 MiB). The 122 plans and 78 specs
under `docs/superpowers/` are historical planning records, not current
commands; they need an archive index rather than being treated as live docs.

### Removal order

1. Freeze this inventory and add an automated reference check for code under
   `tools/riscv/` (not for evidence).
2. Remove the truly independent zero-reference files first: `game/snake.c` and
   `megrez_control_chain.py`; then retire the systemd demo unit
   (`demo-all.sh`, `README-DEMO.md`, `tools/riscv/systemd/` code) and the broken
   daily-use target. Other zero-reference files are coupled to a feature or an
   evidence page and wait for their batch.
3. Inline browser-web's package set, then remove/archive the old profiles and
   their gates while retargeting shared tests.
4. Archive the legacy desktop/NixOS stacks after moving their shared leaves.
5. Rewrite the current-state documentation; keep the evidence directory.
6. Only split files that remain large after deletion.

Every step runs the focused target plus `make test_riscv_host HOST_TEST_JOBS=32`.
A test-count decrease is allowed only when the corresponding feature is
removed or retargeted in the same change, and the baseline manifest records it.

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
