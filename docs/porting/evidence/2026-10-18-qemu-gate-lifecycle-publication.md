# QEMU gate lifecycle publication fix (2026-10-18T13:24:22Z)

## Root cause
The bounded r7 Firefox run exited with an empty `qemu-r1` output directory.
The common systemd-M2 orchestrator only called its backend publisher after the
whole protocol. Therefore an external termination or early host exception could
leave no durable indication that a run was active or why it stopped.

## Change

- `systemd_m2_gate.py` now emits lifecycle states through an optional backend
  hook: `running/start`, `running/snapshot`, `running/validate`,
  `running/prepare`, `running/launch`, and a terminal `complete` or `failed`.
- `rootfs_gate_backend.py` writes `lifecycle.json` with the existing pinned
  atomic fsync+rename primitive; stale lifecycle output is invalidated with the
  other run artifacts.
- Existing injected operations without the hook remain compatible.
- The operator guide documents the lifecycle artifact.

## Verification

- RED test before implementation: the orchestrator lifecycle test observed
  zero states (`AssertionError: 0 not greater than or equal to 2`).
- GREEN: `PYTHONPATH=. python3 -m unittest -q tools.riscv.tests.test_debian_rootfs.DebianSystemdM2GateTests.test_orchestrator_invalidates_stale_pass_and_always_tears_down`
  → `OK`.
- Focused regression set (systemd-M2, Firefox renderer/scene, desktop DRM
  recovery, documentation) → `39 tests`, `OK`.
- `py_compile` and `git diff --check` pass.
- Direct temporary-output check produced a readable `lifecycle.json` with
  `status=running`, `phase=start`.

No QEMU, Firefox, serial device, or physical board was started in this fix.
E3 remains blocked until a later bounded runtime produces durable guest serial
and result evidence.
