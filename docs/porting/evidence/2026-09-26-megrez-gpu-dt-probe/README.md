# Megrez PowerVR device-tree probe

The selected Asterinas boot from `main` commit `2606ed2c2` reported:

```text
ASTERINAS_GPU_DT_PROBE status=ready base=0x51400000 size=0xfffff clocks=3 resets=5 interrupts=1 dma=noncoherent mmio=untouched power=unverified
```

The kernel Image SHA-256 was
`42b6abfd716205b2e1f75d1c3108cff2cdaafe90b36b6cb5214b858dc0b5be8d`.
The prepared Megrez DTB SHA-256 was
`465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba`;
Stage1 SHA-256 was
`ea446515661f2380568426b3c37da0b6a0698fc51f88d32c62807fce04890962`.
RockOS default boot was not changed. U-Boot verified each selected artifact's
length and CRC32 before `booti`. The selected command line included only the
additional `asterinas.gpu_dt_probe=1` diagnostic flag and retained the
420-second Asterinas software recovery timer and local root debug console.

The boot produced a fresh root-console UID 0 and boot ID, then reported a
running Firefox desktop service with a 1920×1080 DRM display path. The host
closed and reopened the serial device and verified the same root boot ID.
The software recovery timer was then disarmed, leaving the board running the
controlled Asterinas desktop. See the [result](result.json) and the
[compressed serial transcript](candidate-boot.serial.log.gz).

The first physical attempt reached the root console but its `dmesg` query
returned no early probe record while `loglevel=off` was in force. It rebooted
and recovered to RockOS. Commit `2606ed2c2` made this opt-in one-line probe
visible on the serial console, and the subsequent selected boot passed.
This result validates the DT resource shape only. It does not establish
GPU power, clock/reset sequencing, firmware execution, a PowerVR render node,
or hardware-accelerated Firefox pixels.
