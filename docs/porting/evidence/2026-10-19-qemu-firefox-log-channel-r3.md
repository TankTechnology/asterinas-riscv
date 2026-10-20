# Firefox diagnostic log channel r3

## Host preflight fix

The Firefox root image has profile `desktop-m4-firefox`. The DRM entrypoint
previously inherited the display-only profile check and rejected that image
before QEMU. `DesktopM4Operations._accepted_profile_identities()` now accepts
both schema-4 `desktop-m4` and `desktop-m4-firefox`; the Firefox-specific
service selection remains conditional on the latter.

Static validation passed:

```text
python3 -m unittest tools.riscv.tests.test_debian_rootfs.DebianDesktopM4GateTests
Ran 26 tests ... OK
python3 -m py_compile ...
git diff --check
```

## Runtime attempt

The corrected `desktop_m4_drm_gate.py` entrypoint constructed the expected
QEMU command containing `-display egl-headless,rendernode=/dev/dri/renderD129`
and `-device virtio-gpu-gl-device`. The persistent QEMU container then exited
with host exit code 0 while lifecycle phase was still `launch`; no serial log,
guest marker, or Firefox state was produced. The lifecycle was atomically
closed as a host failure rather than left as `status=running`.

```text
phase=finished status=failed
reason=host container exited during QEMU launch before serial output
termination.kind=host-container-exit
termination.container_exit_code=0
termination.qemu_serial_artifact=false
```

This is a host-runtime failure and provides no E3 guest result. The logger
serialization change therefore still needs one successful short probe before
it can be considered runtime-verified.

Artifacts:

- lifecycle: `/mnt/mydisk/qemu-firefox-log-channel-r3/lifecycle.json`
- lifecycle SHA-256: `7fadac70975e7e725fe1032b67de29abc54398b8ab0623519ccebd0eb80c0caa`
- QEMU serial artifact: absent
