# Firefox ppoll/futex bounded runtime r99

- gate: `e3-firefox-ppoll-runtime`
- status: fail (bounded protocol failure)
- root image: `/mnt/mydisk/qemu-firefox-probe-r99/debian-root.ext2`
- lifecycle: `/mnt/mydisk/qemu-firefox-probe-r99/lifecycle.json`
- result: `/mnt/mydisk/qemu-firefox-probe-r99/result.json`
- serial: `/mnt/mydisk/qemu-firefox-probe-r99/desktop-m4-drm.serial.log`
- lifecycle SHA-256: `26f192093badd6b05889b53db2fd7991c28b04ba19577bb1b7e40b3a4fa77a02`
- result SHA-256: `d365c1ba7e77b5408f5d3f5b081f074f302dde0da9e880ad84d6190ab35aa7ef`
- serial SHA-256: `3bbc42f13afde050435439758eba7e4d2b9813d584e54c11582cb4104faa9780`

## Observed boundary

- `FIREFOX_DRM_GLXTEST rc=0 ... RENDERER virgl ... DRM_RENDERDEVICE /dev/dri/renderD128` — pass.
- Marionette greeting boundary — pass.
- `WebDriver:NewSession` send and send-complete were emitted, but no NewSession response boundary arrived before the 360-second host budget.
- Process snapshots show the Firefox parent in syscall 73 (`ppoll`) and worker threads in syscall 98 (`futex`); the log contains ppoll counters and a futex wait detail.
- No `FIREFOX_DRM_PROBE_BOUNDARY name=new-session status=pass` and no final Firefox scene receipt were produced; E3 remains blocked.

## Classification

This is a single bounded runtime receipt, not evidence that virgl or Firefox WebRender failed.  The remaining failure is the guest NewSession/IPC boundary; no further full-scene rerun is authorized by this receipt.

| marker | observed |
| --- | --- |
| `glxtest_virgl` | `true` |
| `greeting_pass` | `true` |
| `new_session_send` | `false` |
| `new_session_done` | `false` |
| `ppoll_profile` | `true` |
| `futex_snapshot` | `true` |

- Evidence SHA-256: `92a7548aef65592c3e724b2e9b777277bb2aa30d63dbdfef205f38226d78ab7b`
