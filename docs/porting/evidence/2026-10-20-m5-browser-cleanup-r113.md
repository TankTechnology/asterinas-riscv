# M5/browser-web cleanup r113

- gate: `m5-browser-legacy-cleanup`
- claim: E3 static legacy cleanup
- status: `pass`
- scope: host-only; no QEMU, Firefox runtime, serial, network, or board action

## Closed item

`tools/riscv/debian/rootfs/desktop_m5_evidence.sh` was removed. The reference
scan found its only code/test reference in the legacy
`test_m5_profile_keeps_formal_markers_without_debug_console_flood` test. The
script was not selected by `build_rootfs.sh`, `build_stage1.sh`, a profile,
overlay, systemd unit, Make target, E1–E5 gate, or Megrez management adapter.
The test method was removed with the orphan script. The active browser-web
overlay installs `browser_web_evidence.sh` for the online profile instead.

## Retained items

`browser_web_dev_overlay.json` remains retained because it is selected by the
documented profile-named development-overlay workflow and contains active
browser-web services and management assets. All other `browser_m5_*`,
`browser_web_*`, and `desktop_m5_*` candidates have active profile/build,
Stage1, overlay, or management references. No further file-only deletion is
safe in this round.

## Verification

- reference scanner + deletion manifest validator: pass (`206` candidates)
- `python3 -m compileall -q tools/riscv/debian/rootfs tools/riscv/tests`: pass
- `python3 -m unittest -q tools.riscv.tests.test_debian_browser_web`: pass (`94`)
- `python3 -m unittest -q tools.riscv.tests.test_debian_dev_overlay`: pass (`24`)
- `python3 -m unittest -q tools.riscv.tests.test_refactor_reference_scan`: pass (`3`)
- `git diff --check`: pass

Additional dependent suites were also attempted. `test_megrez_gmac_gate.py`
passed (`33`; its two intentional parser-error cases print usage text), the
daily-use/debug suites passed (`62`, `16`, and `33` tests respectively), and
`test_megrez_debug_desktop.py` now passes (`9`) after its fixture was aligned
with the current `GENERIC_SV39_CPU` contract. The rootfs suite has one
environment-only error because `/usr/local/qemu/bin/qemu-img` is unavailable.
That missing host tool is unrelated to this deletion, and no runtime or
hardware test was started.
