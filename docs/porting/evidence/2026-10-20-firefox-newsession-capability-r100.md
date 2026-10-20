# Firefox NewSession capability boundary r100

- gate: `e3-firefox-newsession-capability`
- status: `pass` (static/unit boundary only)
- claim: E3 diagnostic; this receipt does **not** claim the Firefox scene passed

## Finding and change

The short Marionette probe already sent only
`{"strictFileInteractability": true}`. The full DRM scene still sent the
legacy three-field capability object (`acceptInsecureCerts`,
`pageLoadStrategy`, and `strictFileInteractability`). Runtime r99 reached the
Marionette greeting and completed sending `WebDriver:NewSession`, then produced
no response before its bounded deadline. The scene gate now uses the same
minimal capability object as the short probe; page-load behavior is configured
after a session exists.

This is a guest gate change only. The Debian Firefox binary remains untouched;
no QEMU, Firefox runtime, serial, or hardware operation was performed.

## Verification

| check | result |
| --- | --- |
| Firefox scene and Marionette unit tests | 164 tests, all pass |
| Python static compilation | `python3 -m compileall -q tools/riscv` — pass |
| Shell syntax | `bash -n tools/riscv/debian/rootfs/*.sh tools/riscv/*.sh tools/riscv/xorg/*.sh` — pass |
| whitespace | `git diff --check` — pass |

The next runtime action is one short NewSession-only probe using the updated
scene gate; a full 900-second scene remains deferred until that boundary is
observed.

