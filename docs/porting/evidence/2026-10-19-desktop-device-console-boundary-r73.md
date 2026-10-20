# M4 device-console boundary — r73

- Gate: `desktop-device-console-boundary`
- Scope: bound the ordered device-access diagnostic marker write.
- Code change: `desktop_m3_device_access.sh` now wraps ordered `/dev/console`
  phase writes in `timeout --signal=KILL 1s` and records a volatile fallback
  marker under `/run/asterinas-desktop-device-phase.log`.
- Static/unit result: `make test_riscv_debian_rootfs_unit` 325/325; Firefox
  fast check 325/325; `bash -n`, targeted `py_compile`, and `git diff --check`
  passed.
- The broader 369-test physical-unit entry was rerun after this change; 368
  tests passed and `test_cleanup_kills_group_after_leader_exit` hit its host
  deadline while a stale `sha256sum` was blocked in `/mnt/mydisk` I/O. The
  same 369-test entry was fully green in r72; this r73 run is therefore
  recorded as an environment timeout, not silently counted as green.

## Runtime boundary

The r71 serial receipt showed the last guest marker before the service stalled:

```text
ASTERINAS_DESKTOP_DEVICE_ACCESS phase=input-permissions-deferred
```

The r73 derived rootfs was materialized with SHA-256
`1fa2a486453f72fbb56d109a5045d421ceca806c6f402f5848f6824c288c1a3e`.
The first bounded attempt failed host preflight because the GL-capable QEMU
backend was not selected. After selecting the backend, the container initially
could not load `libEGL.so.1`; installing the runtime library and restoring the
QEMU OpenSBI data-directory links allowed the final attempt to enter the guest.
That attempt reached the Stage1 `handoff-enter action=exec` marker but did not
produce a systemd banner before the 60-second deadline:

```text
DEBIAN_STAGE1_PROGRESS step=handoff-enter action=exec
qemu-system-riscv64: terminating on signal 15 from pid ...
```

The intermediate `libEGL.so.1` and OpenSBI path failures are host-environment
failures; the final timeout is a separate Stage1-to-systemd startup boundary.
An independent 60-second `desktop_m4_gate.py` run with `-display none` reached
the same Stage1 `handoff-enter action=exec` boundary and produced no systemd
banner, so this boundary is not specific to virgl. It is not an E3 guest pass:
no desktop service marker, Firefox, serial write, board, or hardware operation
was performed.
