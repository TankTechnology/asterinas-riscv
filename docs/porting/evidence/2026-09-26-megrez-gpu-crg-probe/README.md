# Megrez GPU clock/reset snapshot

The selected Asterinas boot from `main` commit `9ca5f7f2d` validated the
prepared Megrez GPU DT resources and printed this read-only CRG snapshot:

```text
ASTERINAS_GPU_CRG_PROBE status=observed aclk=0x00000020 cfg=0x00000000 gray=0x00000000 reset=0x00000000 gates=0b000 deasserted=0b00000 crg=read-only gpu_mmio=untouched
```

The EIC7700 clock driver assigns bit 31 of the three words to the `aclk`,
`cfg_clk`, and `gray_clk` gates. Its reset driver clears a bit to assert reset
and sets it to deassert; the GPU uses bits 0–4 of the word at CRG offset
`0x404`. This snapshot therefore shows all three gates off and all five GPU
resets asserted at this point in Asterinas startup. The `aclk` word retains
divider bits `0x20`; the experiment did not change or independently verify
the resulting clock frequency. The source contract is the pinned RockOS
[`eswin_cpu/sysconfig.c`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/system/eswin_cpu/sysconfig.c),
[`clk-eic7700.c`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/clk/eswin/clk-eic7700.c),
and [`reset-eswin.c`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/reset/reset-eswin.c)
at commit `bf2ec5d53002c16bc1bc593b92516eb6c2866176`.

The Image SHA-256 was
`5b8896a4f935952cad36138acdb5fc0a6be07aa4ac4df550734ea6aab237bf41`.
The prepared DTB and Stage1 SHA-256 values were
`465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba`
and `ea446515661f2380568426b3c37da0b6a0698fc51f88d32c62807fce04890962`.
Host and RockOS SHA-256 matched before boot; U-Boot verified selected file
lengths and CRC32. The default RockOS boot entry was not changed.

The same selected boot reached a fresh UID-0 debug-console boot ID, a running
Firefox service, and a 1920×1080 DRM display path. The host closed and
reopened serial, verified the same root boot ID, and disarmed the software
recovery timer. The board was left at the controlled Asterinas desktop.
See the [result](result.json) and
[compressed serial transcript](candidate-boot.serial.log.gz).

No CRG or GPU register was written, and no GPU register was read. This is
evidence for the initial powered-off state, not a PowerVR render driver or
accelerated Firefox result.
