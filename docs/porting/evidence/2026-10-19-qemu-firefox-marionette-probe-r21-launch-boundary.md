# Firefox Marionette probe r21 launch boundary

## Result

This was the single short-probe runtime using the sample-timeline rootfs.  The
host runner stopped before a QEMU child produced serial output.  The output
directory contains only the boot image, the copied root image, and the
atomically finalized lifecycle file; it contains no serial log or `result.json`.

This is a host launch failure, not evidence about Firefox, NewSession, DRM, or
WebRender.  It is therefore recorded as `inconclusive` and will not be used to
change the E3 guest diagnosis.

## Lifecycle evidence

```text
phase=finished
status=failed
reason=host runner ended before QEMU serial output; no guest probe started
termination=no qemu child or serial/result artifact remained after host session ended
```

The lifecycle file was finalized with write/`fsync`/rename/parent-`fsync` after
confirming that no `qemu-system-riscv64` or gate process remained.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| lifecycle | `980cd1437a2e211604b8feb7e54c18883500627c8c02ddbe2599c5e48202f64a` |
| copied root image | `d0ee75ef938cbf548b10b04e16e34f76809901bdcafcc4c068b72a4a878f8aa9` |
| overlay root image input | `f3a8796c3f3c842ca3614e13fe6e35a02afca1d5a9bf8868f81e18b929ef2d2a` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r21-container/`

