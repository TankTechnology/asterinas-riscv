# QEMU DRM host preflight: r10

Date: 2026-10-17 (Asia/Shanghai)

This is a host-side, fail-closed launch-boundary change. No physical board
serial input, MMIO access, reboot, or display experiment was performed.

## Change

`ConcreteOperations.launch()` now calls `_validate_qemu_argv()` after building
the complete command and before creating the QEMU process. The default hook is
a no-op. `DesktopM4DrmOperations` overrides it and runs
`tools.riscv.drm.virgl_host_preflight.inspect_qemu(argv[0])`; a missing
`egl-headless` backend, missing `virtio-gpu-gl-device`, failed help query, or
missing executable raises `GateFailure` before `launch_process()`.

## Verification

| Gate | Command | Result |
| --- | --- | --- |
| TDD fail-closed branch | `PYTHONPATH=. python3 tools/riscv/tests/test_debian_rootfs.py DebianDesktopM4GateTests.test_drm_qemu_host_preflight_fails_closed` | PASS (1 test) |
| Desktop M4 regression | `PYTHONPATH=. python3 tools/riscv/tests/test_debian_rootfs.py DebianDesktopM4GateTests` | PASS (23 tests) |
| Existing host preflight tests | `PYTHONPATH=. python3 -m unittest discover -s tools/riscv/tests -p 'test_drm_virgl_host_preflight.py'` | PASS (4 tests) |
| Syntax | `python3 -m py_compile ...rootfs_gate_backend.py ...desktop_m4_gate.py ...test_debian_rootfs.py` | PASS |

The actual host probes were intentionally run as well:

```text
--qemu /root/qemu-gl-src/build/qemu-system-riscv64
passed=false reason=qemu --version failed: PermissionError: [Errno 13] Permission denied

--qemu qemu-system-riscv64
passed=false reason=qemu --version failed: FileNotFoundError: [Errno 2] No such file or directory
```

The first path is the GL build used by the prior container evidence but is not
executable from this host namespace. The second path is absent. Therefore no
new runtime QEMU DRM/virgl pass is claimed by this receipt; a GL-capable QEMU
container or an executable host path is still required for the next runtime
gate.

## Physical boundary

The board remained in RockOS and was queried read-only over SSH. The current
read-only receipt is `/mnt/mydisk/board-readonly-20261017/README.md`; it shows
SSH/ICMP reachability, UID 1000, `/dev/fb0`, and no new boot-id. No physical
DRM/GPU/fb0 experiment was started.

