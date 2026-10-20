# QEMU scanout contract (2026-10-13)

This receipt is simulation-only. It does not claim DRM, PowerVR, or physical
HDMI output.

## Contract

The Asterinas QEMU fbdev profile uses the Bochs PCI display aperture
`0x40000000..0x40ffffff` with a `1280x1024` `x8r8g8b8` surface and a
`5120`-byte stride. `tools/riscv/qemu_scanout_contract.py` is the host-side
validator for this layout, P6 screenshot geometry, RGB regions, and the pixel
payload SHA-256. It fails closed for a missing/short BAR, an invalid stride,
a truncated mapping, truncated PPM data, or a pixel readback mismatch.

## QTest and QMP run

The custom QEMU source is `/home/ubuntu/.codex/qemu-eic7700-10.2.1`. The
test is `tests/qtest/bochs-display-test.c`, registered for RISC-V in
`tests/qtest/meson.build`. It was built in the persistent
`qemu-eic7700-builder-20261004` container and run with:

```text
docker exec -e QTEST_QEMU_BINARY=/root/qemu/build/qemu-system-riscv64 \
  qemu-eic7700-builder-20261004 \
  /root/qemu/build/tests/qtest/bochs-display-test --tap
```

Result: three QTest cases passed (`scanout-contract`, `missing-bar`, and
`invalid-mode`). The scanout case wrote fixed red, green, and blue regions,
captured them through QMP `screendump`, and calculated this RGB pixel hash:

```text
e1b912e938b2c8829473ac082198b58c3a2145c9fa86524fe02e14b86551c39b
```

The current QEMU build is TCG-only. The result validates the emulated BAR and
QMP scanout path; it does not represent board GPU timing or hardware DRM.

## Asterinas handoff gate

`desktop_m4_gate.py` now requires one ordered U-Boot `simple-framebuffer`
metadata sequence (or the corresponding kernel registration) matching this
contract:

```text
Registered firmware framebuffer: base=0x40000000, size=0x1000000, resolution=1280x1024, stride=5120, format=BgrReserved
```

Repeating the U-Boot node creation or kernel registration fails as a duplicate
owner, which catches a second takeover of the firmware aperture. The existing
`/dev/fb0` 16x16 write/read/restore
helper, Xorg fbdev markers, QMP screenshot gate, and software-reboot recovery
gate remain the execution path; no second desktop launcher was introduced.

Focused host tests passed:

```text
python3 -m unittest -q \
  tools.riscv.tests.test_qemu_scanout_contract \
  tools.riscv.tests.test_debian_desktop_m4_x11_gate \
  tools.riscv.tests.test_debian_rootfs.DebianDesktopM4GateTests \
  tools.riscv.tests.test_debian_desktop_m4_framebuffer_probe \
  tools.riscv.tests.test_debian_desktop_m4_framebuffer_rw
Ran 29 tests ... OK
```

The handoff classifier is deliberately stricter than the older receipts. A
previous desktop receipt without either the ordered U-Boot metadata sequence
or the kernel registration line must be replayed before it can be called a
current pass. The retained QEMU canary serial was reclassified with the new
gate and passed the handoff check; its existing result remains
`qualification=fbdev-pattern-only` and `application_paint_verified=false`.
The retained X11 boundary receipt separately has 86 screenshot colors and
614350 non-background pixels. These receipts establish the software fbdev to
QEMU scanout boundary; they do not claim a physical display or PowerVR DRM.
