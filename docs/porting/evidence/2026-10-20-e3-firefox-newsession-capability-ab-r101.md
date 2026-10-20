# Firefox NewSession capability A/B runtime probe r101

- gate: `e3-firefox-newsession-capability-ab`
- claim: E3 diagnostic; this is a NewSession-only probe and does not claim the full scene passed.
- status: `fail` for the requested boundary; both arms reached the same no-response boundary.

## Scope

Two serial QEMU runs used the same kernel, U-Boot, DTB, Stage1 initramfs, rootfs image, rootfs manifest, package lock and package checksums. Only `ASTERINAS_FIREFOX_NEWSESSION_CAPS` changed. No Firefox binary, kernel source, real hardware, serial write, or full 900-second scene was used.

Common rootfs:
`/mnt/mydisk/asterinas-firefox-rootfs-20261020-probe-ab-r101/debian-root.ext2` (SHA256 `f876af04b4d649b1bbf8eaf98fddd5946ea411bc10821e56eca5a3c26b4e3b79`)

## Marker comparison

| arm | capability object | GLX/virgl precondition | Marionette greeting | NewSession response | terminal marker |
| --- | --- | --- | --- | --- | --- |
| A | `{"strictFileInteractability": true}` | pass; `RENDERER virgl`, `renderD128` | pass | absent at 360s | `FIREFOX_DRM_PROBE_DONE` absent |
| B | `{"acceptInsecureCerts": false, "pageLoadStrategy": "none", "strictFileInteractability": true}` | pass; `RENDERER virgl`, `renderD128` | pass | absent at 360s | `FIREFOX_DRM_PROBE_DONE` absent |

Both serial logs contain the bootarg marker `ASTERINAS_FIREFOX_NEWSESSION_CAPS=minimal` or `...=legacy`, proving the arm selection reached the guest. Both terminal snapshots show Firefox parent/worker wait states (`ppoll`/`futex`) and no response header.

## Runtime artifacts

### A-minimal
- lifecycle: `/mnt/mydisk/qemu-firefox-probe-ab-r101-A-minimal/lifecycle.json` (SHA256 `99336f0551c48eefba4351995263817954990fbdd33368400d1a80d16d757b59`)
- result: `/mnt/mydisk/qemu-firefox-probe-ab-r101-A-minimal/result.json` (SHA256 `1a07c8bdb93c83a8d525b86d23d74870434bfaa91fce6460915aaeffea97da9b`)
- serial: `/mnt/mydisk/qemu-firefox-probe-ab-r101-A-minimal/desktop-m4-drm.serial.log` (SHA256 `a30c5e7d8c244fee5875907f65227953b9c5e525e2ef70f16f8f1cc2e5d85caf`)
- observed markers: `{'glxtest': 'pass (RENDERER virgl, DRM_RENDERDEVICE /dev/dri/renderD128)', 'greeting': 'pass', 'new_session': 'no response at 360s', 'done': 'absent'}`

### B-legacy
- lifecycle: `/mnt/mydisk/qemu-firefox-probe-ab-r101-B-legacy/lifecycle.json` (SHA256 `8b584f400c663d436dd8909b6bf83cc6983cad32cf1cfd216136c93863a402d5`)
- result: `/mnt/mydisk/qemu-firefox-probe-ab-r101-B-legacy/result.json` (SHA256 `3f14b265173319ceac423e70b29c79eb9698dc951cede0a1e60ea7f3b50c1737`)
- serial: `/mnt/mydisk/qemu-firefox-probe-ab-r101-B-legacy/desktop-m4-drm.serial.log` (SHA256 `5e2ce4d7aabca82d922fbfe11fa136805835b58a5a9ceee98390af07d4fa80e9`)
- observed markers: `{'glxtest': 'pass (RENDERER virgl, DRM_RENDERDEVICE /dev/dri/renderD128)', 'greeting': 'pass', 'new_session': 'no response at 360s', 'done': 'absent'}`

## Interpretation

Neither arm obtains `WebDriver:NewSession`; the historical three-field capability success is not reproduced on this exact current image/kernel/DTB. Since the only changed variable did not change the boundary, capability negotiation is not sufficient to explain r99/r100. The next bounded investigation is the already observed guest IPC wait boundary (`ppoll` in the Firefox parent and `futex` in a worker), without another capability or full-scene run.

## Budget note

The requested 180-second per-arm budget is below the current gate contract and measured TCG cold-start envelope. The runs therefore used the existing 360-second host budget (`PROBE_TIMEOUT=300` plus recovery margin), and this deviation is recorded rather than silently presenting a 360-second run as a 180-second result.
