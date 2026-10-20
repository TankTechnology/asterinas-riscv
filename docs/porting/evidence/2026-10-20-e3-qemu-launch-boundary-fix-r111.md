# E3 QEMU launch-boundary fix r111

The two trace attempts showed that the host could remain in
`lifecycle.phase=launch` with a live or zombie QEMU and no serial/result
artifact. The launch path now bounds the QEMU HMP monitor-connect/prompt phase
to `min(boot_timeout, 60s)`. A missing monitor is classified as a host launch
failure and enters the existing process-group cleanup path before any guest
protocol budget is consumed.

Verification:

- `py_compile` passed for `rootfs_gate_backend.py`.
- `DebianRootfsGateBackendSessionTests.test_session_uses_hardlinked_same_root_and_removes_directory_after_drain` passed.
- `DebianRootfsGateRuntimeTests.test_cleanup_escalates_term_to_kill_for_stubborn_group` passed.
- `git diff --check` passed for the changed host/runtime files.

No QEMU or hardware was started for this fix. The next trace run is permitted
only after the launch-boundary fix is present and must produce a terminal
`result.json` or an explicit host launch failure receipt.

