# Desktop service single-entry gate — r69

## Assertion

The M4 desktop image has one service entry without a PAM/TTY login handoff;
the image no longer installs a second `*-ordered.service` selected by
`ConditionKernelCommandLine`.  This removes the service-selection boundary
identified by r66/r68, where Asterinas systemd started the PAM/TTY unit before
`ExecStart` and never selected the ordered sibling.

This is a **static/unit receipt**.  It does not claim a QEMU or hardware boot.

## Change under test

- `tools/riscv/debian/rootfs/build_rootfs.sh`
  - keeps PAM/TTY options for M3 and M5 online;
  - emits M4 without `PAMName`, `TTYPath`, `StandardInput`, or
    `ConditionKernelCommandLine`;
  - removes the duplicate M4 ordered unit and its second graphical-target
    symlink;
  - sets the M4 session identity mode to `ordered` deterministically.
- `tools/riscv/debian/rootfs/desktop_m4_session.sh`
  - removes `-keeptty vt1` from both Xorg launch paths.
- `tools/riscv/tests/test_debian_rootfs.py`
  - asserts the single M4 unit and the absence of the PAM/TTY handoff.
- `tools/riscv/tests/test_debian_browser_web.py`
  - removes an assertion for a deleted Make target and verifies M4 has no
    `keeptty` argument.

## Validation

| Check | Result |
| --- | --- |
| `make test_riscv_debian_rootfs_unit` | PASS — 325 tests |
| `bash tools/riscv/firefox_fast_check.sh` | PASS — 325 tests |
| `make test_riscv_firefox_daily_use_physical_unit` | PASS — 369 tests |
| `bash -n` for builder/session/fast-check scripts | PASS |
| `python3 -m py_compile` for changed Python files | PASS |
| `refactor_reference_scan.py --verify` | PASS — 214 candidates |
| `git diff --check` | PASS |

## Boundary

The latest runtime evidence remains `r68`:
`docs/porting/evidence/2026-10-19-desktop-ordered-unit-replay-r68.md`.
No new runtime result is inferred from this receipt.  The next runtime action,
when authorized, is one bounded M4 desktop replay to verify that the single
service reaches `ASTERINAS_DESKTOP_M4_SESSION phase=start`.
