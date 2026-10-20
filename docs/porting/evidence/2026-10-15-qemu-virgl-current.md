# Current QEMU virgl protocol gate — 2026-10-15

This is a host/QEMU-only receipt. It does not access the EIC7700 board, write
serial input, reboot hardware, or claim native PowerVR acceleration.

The guest initramfs was rebuilt from the current workspace with
`tools/riscv/drm/build_virgl_param_gate.sh`:

```text
159ccd2f0f43b429f453597a7d1cd0e2559e93965109b59d82835ce6410e5838  target/drm-virgl/current-20261015-initramfs.cpio.gz
```

The private QEMU input is
`target/qemu-uboot/drm-current-virgl-20261015/`. It uses the current render
node kernel/DTB input, the rebuilt initramfs, and a private copy of the boot
disk. The manifest and boot disk hashes are:

```text
9605ba2f48785f98aeb12590fee324ec406a4271babe1395535f6d38cad3f401  artifacts.json
0f6b4a84824698615b14b47d7960d476b610713c95a60f04d9924ad1b8292d33  boot.ext4
```

The selected QEMU is the GL-enabled build in
`asterinas/qemu-gl-env:20261014`:

```text
QEMU emulator version 10.2.1
egl-headless: present
virtio-gpu-gl-device: present
```

The host preflight passed before the guest started. The guest gate command was
run with `PATH=/root/qemu-gl-src/build:/usr/local/qemu/bin:$PATH` and returned
`passed=true`:

```json
{
  "backing_us": 1757,
  "caps_bytes": 308,
  "caps_context_us": 2895,
  "expected_3d": 1,
  "passed": true,
  "reason": "passed",
  "reported_3d": 1,
  "reported_capsets": 2,
  "resource_bo": 1,
  "resource_res": 2,
  "resource_us": 137352,
  "submit_us": 2545
}
```

The ordered serial receipt is:

```text
DRM_VIRGL_PARAM 3d=1 capsets=0x2
DRM_VIRGL_CAPS PASS caps_bytes=308
DRM_VIRGL_CONTEXT PASS
DRM_VIRGL_RESOURCE PASS bo=1 res=2 size=4096
DRM_VIRGL_SUBMIT PASS refused=0
DRM_VIRGL_FENCE PASS
DRM_VIRGL_BACKING PASS
DRM_VIRGL_TIMING caps_context_us=2895 resource_us=137352 submit_us=2545 backing_us=1757
DRM_VIRGL_IMPLICIT PASS
ASTERINAS_DRM_VIRGL_R1_READY
```

Evidence hashes:

```text
6850609e7cb6bc30b582b130e9936a590b7ca5a152c6d653a00583a2a476e527  virgl-evidence/serial.log
2ef4e42180daf1c066b505c1340d8155924d54af160cfa4c62d5b062458c7fb2  virgl-evidence/result.json
```

The gate proves the current guest's render-node protocol path through capset
query, context creation, a 4096-byte `PIPE_BUFFER` resource, backing/mmap,
submission, a completed fence, and a contextless Mesa-style submission. It
does not prove an Xorg glamor renderer, EGL/GLES pixels, Firefox WebRender, or
the board's hardware GPU. The remaining QEMU desktop phase must run with the
same GL QEMU and require `renderer=virgl`; the existing software `llvmpipe`
receipts remain separate evidence.
