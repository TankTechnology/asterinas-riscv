# M4 ordered fbdev metadata boundary (r78)

- Gate: `desktop-fbdev-stat-boundary`
- Scope: remove an unbounded ordered-boot `stat /dev/fb0` before Xorg owns the
  provider; no Firefox, network, DRM, serial write, or physical-board action.
- Status: `code-pass`; runtime desktop readiness remains open.

## Root cause

The ordered fbdev branch of `desktop_m4_session.sh` already deferred the
read/write open, but still executed `stat /dev/fb0` together with the input
nodes.  Asterinas can block even on fb0 metadata operations while the display
owner is being handed off.  The r69/r76 serial traces both stopped after
`ASTERINAS_DESKTOP_M4_SESSION phase=start`, before `phase=provider-fbdev`.

## Change

For `INPUT_IDENTITY_MODE=ordered` and `DISPLAY_PROVIDER=fbdev`, the session now
emits `DESKTOP_M4_DEVICE framebuffer-stat=deferred` and
`framebuffer-open-rw=deferred` without touching `/dev/fb0`.  Non-ordered
physical boots retain the original `stat` and read/write-open behavior.

## Verification

| Check | Result |
| --- | --- |
| `test_debian_browser_web` | PASS, 95 tests |
| `make test_riscv_debian_rootfs_unit` | PASS, 327/327 |
| Python compile, shell syntax, `git diff --check` | PASS |
| r78 overlay materialization | PASS; root image `a4a4bd25241a5796ea9efc938cf8bca89e58bda2a7813df746e26dca7738504d` |
| r78 60s runtime | inconclusive; no systemd banner before timeout |
| r78 120s runtime | fail-closed `passed=false`, `reason=protocol`; reached the systemd banner but emitted no desktop marker |

Runtime receipts:

- `/mnt/mydisk/qemu-desktop-m4-r78-stage1/result.json`
- `/mnt/mydisk/qemu-desktop-m4-r78-stage1-120/result.json`

The 120-second serial tail ends after `systemd`'s banner and the stage1
`handoff-enter action=exec`; it does not prove that the new session branch ran.
The next bounded gate must therefore instrument the post-handoff systemd
activation boundary rather than claim the desktop is fixed.
