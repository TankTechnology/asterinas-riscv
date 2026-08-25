# Current-main DRM R1 QEMU evidence

Date: 2026-08-26

## Result

The `codex/drm-r1-current-main` candidate at `5cabea2d7` passed one bounded
QEMU 10.2.1 boot using the registered `generic-sv39-drm-cursor-smp4` profile.
The run reached Asterinas userspace in 12.001 seconds and exited cleanly after
the terminal marker. The prepared run reported `PASS`, `BOOT_COMPLETED`, four
harts, Sv39, 2 GiB, `-nic none`, and one `virtio-gpu-device`.

The exact ordered cursor evidence was:

```text
virtio_gpu_update_cursor scanout 0, x 32, y 24, update, res 0x2
DRM_CURSOR_SET PASS
virtio_gpu_update_cursor scanout 0, x 96, y 64, move, res 0x0
DRM_CURSOR_MOVE PASS
virtio_gpu_update_cursor scanout 0, x 96, y 64, update, res 0x0
DRM_CURSOR_HIDE PASS
ASTERINAS_DRM_CURSOR_R1_READY
```

QEMU exposes one `virtio_gpu_update_cursor` trace event for both
`UPDATE_CURSOR` and `MOVE_CURSOR`; its `update`/`move` field distinguishes the
command. The gate therefore requires exactly two update records and one move
record rather than referring to a nonexistent `virtio_gpu_move_cursor` event.

## Commands

All build and runtime commands ran locally in the pinned
`asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached` container. The
essential repository commands were:

```bash
tools/riscv/drm/build_cursor_gate.sh \
  target/qemu-uboot/drm-cursor/initramfs.cpio.gz
make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode \
  CONSOLE=ttyS0 LOG_LEVEL=info

ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
ASTERINAS_INITRAMFS="$PWD/target/qemu-uboot/drm-cursor/initramfs.cpio.gz" \
QEMU_UBOOT_PROFILE=generic-sv39-drm-cursor-smp4 \
QEMU_UBOOT_OUT_DIR="$PWD/target/qemu-uboot/drm-cursor/prepared" \
tools/riscv/prepare_qemu_uboot_booti.sh prepare

PYTHONPATH=tools/riscv python3 -m drm.cursor_gate \
  --uboot "$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  --boot-disk "$PWD/target/qemu-uboot/drm-cursor/prepared/boot.ext4" \
  --manifest "$PWD/target/qemu-uboot/drm-cursor/prepared/artifacts.json" \
  --output-directory "$PWD/target/qemu-uboot/drm-cursor/evidence"
```

The QEMU run used `--network=none` at the container boundary. No remote CI was
observed or used as evidence.

## Artifact identities

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| Asterinas Image | 14,045,216 | `dcc17423958e40af7b42bf277613f6c26dc1ea7ed3d5391edb800a242db9868f` |
| cursor initramfs | 283,452 | `48e8e7d5770d4a07558db0482a0f44c36e85e2c1b235288df50ac89ac6c8df3a` |
| U-Boot | 9,142,960 | `05e6bd2bf3c437ce41a9de1719b189102ced2530faf0bb38ea1fdfabb6c54b75` |
| generated DTB | 8,204 | `54039005a3ebddb526df7e0b109b2ee143e051b08bb8a26034ae7f4ae3549815` |
| boot.ext4 | 67,108,864 | `a35ceab2e0dc4cdcc1ee2ca002f48999cba4df9288b83791219f91cf17df2f2a` |
| serial.log | 26,931 | `4bf30a89e51e787e57f27b4eafd063c20d08f0d22e0a4ddce228b127e145c04a` |
| cursor result.json | 95 | `c99809d038288da82debffba124b69e9ed9d0d98e6e5f0fc1cc9533a37e7737e` |

## Verification and non-claims

The new cursor unit suite passed 10 tests. The existing U-Boot contract and
runner suites passed 229 tests with one supported cross-compiler skip. The
guest init is a statically linked RISC-V ELF inside a deterministic initramfs.

This result proves the current-main Linux DRM cursor ioctls, dumb-buffer
backing, VirtIO-GPU cursor queue, and QEMU device behavior on generic Sv39
SMP=4. It does not prove Megrez HDMI scanout, EIC7700 display clocks/resets,
physical framebuffer handoff, GPU acceleration, GEM render nodes, virgl,
atomic modesetting, PRIME, or desktop performance.
