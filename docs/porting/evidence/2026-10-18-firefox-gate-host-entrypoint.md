# Firefox DRM gate host entrypoint (2026-10-18)

The host-side Firefox scene gate previously failed before argument handling or
guest startup when invoked directly from the repository root:

```text
python3 tools/riscv/debian/rootfs/firefox_drm_scene_gate.py --help
ModuleNotFoundError: No module named 'tools'
```

The guest path (`/usr/lib/asterinas`) is unchanged. The host path now derives
the repository root from `__file__` and inserts it into `sys.path`, so direct
invocation and package-based unit tests use the same imports. This is an
entrypoint fix only; it does not start Firefox, QEMU, or access the board.

## Verification

```text
python3 -m unittest tools/riscv/tests/test_firefox_drm_scene_gate.py
....................
----------------------------------------------------------------------
Ran 20 tests in 0.004s

OK
```

The direct `--help` probe is intentionally not used as a runtime gate because
this script has no help-only mode and would otherwise enter its real Firefox
workflow. The import path is covered by the same module import used by the 20
tests.

