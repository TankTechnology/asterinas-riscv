# Megrez PowerVR powered identity probe

The selected Asterinas boot from `main` commit `311b7f676` enabled the
independent `asterinas.gpu_powered_id_probe=1` gate. The probe checked the
prepared GPU and CRG device-tree resources, observed the previously measured
powered-off CRG state, enabled the three GPU clocks, released the five GPU
resets in the order used by RockOS, read `RGX_CR_CORE_ID` once, and restored
the original CRG words. The serial result was:

```text
ASTERINAS_GPU_CRG_PROBE status=observed aclk=0x00000020 cfg=0x00000000 gray=0x00000000 reset=0x00000000 gates=0b000 deasserted=0b00000 crg=read-only gpu_mmio=untouched
ASTERINAS_GPU_POWERED_ID status=starting
ASTERINAS_GPU_POWERED_ID raw=0x001e000301980065
ASTERINAS_GPU_POWERED_ID status=validated bvnc=30.3.408.101 crg_restored=1 firmware=untouched render=unavailable
```

The raw ID matches the expected B.V.N.C fields from the pinned RockOS
Volcanic driver. The `crg_restored=1` result means the probe read back the
exact initial clock/reset tuple after restoration. The probe did not load
firmware, submit work, create a render node, or accelerate Firefox. The
existing `/dev/dri/renderD128` belongs to Asterinas's display DRM device,
not to a PowerVR driver. The current Firefox launch wrapper explicitly sets
`MOZ_AVOID_OPENGL_ALTOGETHER=1`, and this root image has no `rgx` firmware.
An additional nonce-framed root check after the validated boot found `card0`
and `renderD128`, but no `/lib/firmware/rgx*`, `eglinfo`, or `glxinfo`.
None of those conditions changed in this probe. The
source contract is RockOS commit
`bf2ec5d53002c16bc1bc593b92516eb6c2866176`: its
[`sysconfig.c`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/system/eswin_cpu/sysconfig.c)
controls the clocks/resets, and
[`rgxbvnc.c`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/devices/rgxbvnc.c)
reads the ID register at GPU offset `0x20`.

The validated Image SHA-256 was
`f0ee60c65fadc75aad427f78700545159b68333b6ca31a4eac271cb1e2be5344`.
The DTB and Stage1 SHA-256 values were
`465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba`
and `ea446515661f2380568426b3c37da0b6a0698fc51f88d32c62807fce04890962`.
Host and RockOS SHA-256 matched; U-Boot verified the loaded sizes and CRC32.
The default RockOS boot entry was left unchanged. The selected Asterinas boot
reached a fresh UID-0 debug-console boot ID
`1d665b36-6fe2-4fbf-92f7-11d7964ca650`, a running Firefox service, and an
Xorg `1920x1080` mode log. The host closed and reopened the serial connection,
verified the same root boot ID, and disarmed the software recovery timer. No
HDMI capture was available to assert pixel-level display correctness. See the
[validated result](validated-result.json) and
[compressed serial transcript](validated-boot.serial.log.gz).

The first selected boot, from commit `f63748815` (Image SHA-256
`49a72d32f3abe68edce8ce3ecf06418726bdb79c966c6d5dd5f6188766dc494b`),
stopped before touching GPU MMIO:

```text
ASTERINAS_GPU_POWERED_ID status=skipped reason=clock_registers_unavailable
```

`IoMem` does not currently recycle an acquired MMIO interval when dropped.
The separate read-only CRG probe had reserved the same interval earlier in
that boot, so the powered probe's second acquisition failed. Commit
`311b7f676` made the two selected probes share one mapping. The failed
candidate automatically recovered to the RockOS login prompt, and RockOS
`6.6.87` was verified over SSH before the retry. See the
[first result](first-result.json) and
[first compressed serial transcript](first-boot.serial.log.gz).
