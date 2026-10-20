# Storage floor preflight r115

- gate: `host-storage-floor-preflight`
- status: `pass` for inventory; no deletion performed
- requirement for the next runtime probe: `/mnt/shared` free space >= 60G
- observed: 58G free (`df -h /mnt/shared`)

The largest workspace candidates are old generated root images, each 2 GiB;
the first twenty candidates are listed below. Literal receipt references were
searched in `docs/porting/receipts`; none of these paths is directly named by a
receipt. This is only a candidate inventory: directory-level manifest and
artifact-family review is still required before deletion.

```text
target/qemu-uboot/drm-desktop-current{,-v2..-v8,-v10..-v12}/debian-root.drm.ext2
target/qemu-simulate-mykernel2/native/debian-root.run.ext2
target/qemu-simulate-dnsenv/native/debian-root.run.ext2
target/qemu-gate-mouse/debian-root.run.ext2
target/qemu-browser-web-playback-v2/debian-root.run.ext2
target/megrez-desktop-boot/simulate-playback-v2/native/debian-root.run.ext2
target/firefox-daily-use-physical/qemu-qualification-merged/debian-root.run.ext2
target/e3-render-policy-r1/rootfs/debian-root.ext2
target/e3-render-policy-r1/rootfs-r8/debian-root.ext2
target/e3-render-policy-r1/qemu-r6/debian-root.run.ext2
target/e3-render-policy-r1/qemu-r5/debian-root.run.ext2
```

No QEMU, Firefox, serial, network, or hardware operation was started. No file
was removed.
