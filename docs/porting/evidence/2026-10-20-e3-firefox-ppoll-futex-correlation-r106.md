# Firefox NewSession ppoll/futex correlation r106

- gate: `e3-firefox-ppoll-futex-correlation`
- claim: E3 diagnostic; this is a read-only analysis of existing serial artifacts
- status: `pass` for boundary classification; it does not claim the Firefox scene passed
- manifest SHA-256: `68645b154bb876db055a7e49b32a55b0f26e3a4bab44e89be4b2223f7abd39f8`

## Evidence compared

No QEMU or Firefox process was started. The analysis reads the existing A/B serial logs from r101 and the prior r99 log. All three runs reached the same transport boundary: greeting response, `WebDriver:NewSession` `send_complete`, and no NewSession response header or terminal marker.

| run | Firefox main process sample | waiting thread | futex thread | GL/Marionette boundary | serial SHA-256 |
|---|---|---|---|---|---|
| A minimal | pid 309 | tid 332, `syscall=73` (`ppoll`) | tid 434, `syscall=98` (`futex`), `arg0=274568377208`, `arg1=393` | greeting pass; virgl/renderD128 pass; NewSession response absent | `a30c5e7d8c244fee5875907f65227953b9c5e525e2ef70f16f8f1cc2e5d85caf` |
| B legacy | pid 301 | tid 321, `syscall=73` (`ppoll`) | tid 433, `syscall=98` (`futex`), `arg0=274568378744`, `arg1=393` | greeting pass; virgl/renderD128 pass; NewSession response absent | `5e2ce4d7aabca82d922fbfe11fa136805835b58a5a9ceee98390af07d4fa80e9` |
| r99 minimal | pid 319 | tid 334, `syscall=73` (`ppoll`) | tids 443/444, `syscall=98` (`futex`), `arg1=393` | greeting pass; virgl/renderD128 pass; NewSession response absent | `3bbc42f13afde050435439758eba7e4d2b9813d584e54c11582cb4104faa9780` |

## Conclusion

The capability object is not the differentiating variable: both A and B have identical post-send wait topology. The latest concrete boundary is guest Firefox IPC/scheduling—one Firefox thread remains in `ppoll` while a worker remains in `futex`—after the request has been fully written. The generic futex/ppoll reproducer r105 passes, so this is not yet evidence for a global `pause_timeout` or futex lost-wakeup bug. The next code-level investigation should instrument the Firefox-specific socket/IPC wake path or compare the worker's futex address against its owning subsystem; do not repeat capability arms or a 900-second scene.

## Sources

- `/mnt/mydisk/qemu-firefox-probe-ab-r101-A-minimal/desktop-m4-drm.serial.log`
- `/mnt/mydisk/qemu-firefox-probe-ab-r101-B-legacy/desktop-m4-drm.serial.log`
- `/mnt/mydisk/qemu-firefox-probe-r99/desktop-m4-drm.serial.log`
- `docs/porting/evidence/2026-10-20-e3-firefox-newsession-capability-ab-r101.md`
- `docs/porting/evidence/2026-10-20-firefox-ppoll-runtime-r99.md`
