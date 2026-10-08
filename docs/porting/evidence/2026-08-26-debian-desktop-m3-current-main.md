# Debian Desktop M3 current-main QEMU evidence

Date: 2026-08-26

Candidate branch: `codex/drm-r1-current-main`

## Result

The signed Debian Trixie `riscv64` Desktop M3 profile cold-booted on Asterinas
and reached a non-root Xorg fbdev session with Matchbox and xterm. The bounded
gate returned `passed: true`, captured a 1280x1024 framebuffer, drained the
serial transcript, and removed its QEMU process group and container.

The observed desktop milestones, in order, were:

```text
DEBIAN_DESKTOP_M3_UDEV state=active
DEBIAN_DESKTOP_M3_LOGIND state=active
DEBIAN_DESKTOP_M3_SESSION user=asterinas tty=tty1
DEBIAN_DESKTOP_M3_INPUT keyboard=evdev pointer=evdev
DEBIAN_DESKTOP_M3_XORG framebuffer=fbdev display=:0
DEBIAN_DESKTOP_M3_CLIENTS window-manager=matchbox terminal=xterm
DEBIAN_DESKTOP_M3_READY user=asterinas display=:0
```

The screenshot visibly contains the `Asterinas Debian` Matchbox window, an
`asterinas@...` xterm prompt, and the pointer. The pixel classifier reported
1280x1024, 163 distinct sampled colors, and 1,308,128 non-background pixels.

## Frozen inputs

| Input | SHA-256 |
| --- | --- |
| current-main Sv39 Asterinas Image | `dcc17423958e40af7b42bf277613f6c26dc1ea7ed3d5391edb800a242db9868f` |
| four-hart QEMU DTB | `54039005a3ebddb526df7e0b109b2ee143e051b08bb8a26034ae7f4ae3549815` |
| U-Boot | `3c11d5ca4cda470fe88bc5b3f335f9f496e395d1b5d298daa40875abdff8bdd8` |
| systemd Stage1 initramfs | `27ac53d8c759b99aa67e3c26109e3839dbe5ebd090008eb0a229295c33394e4b` |
| Desktop M3 ext2 | `a0d593defc60e5e55f78a2b0777a3225d08892ed2f43f089bb8da3cb8b4d0b9c` |
| Desktop M3 manifest | `2470a1cf5dd47fa8aaeba0db955c76105ad7202c5dbf7117188555f014ab16d2` |
| Desktop M3 packages lock | `8de363f5e5dd24c0897e5364f4da2c94be7e108992b1e5437ee12c6ea36abe5a` |
| package checksums | `b28d7e31b10c4ce6455a97b93f25b7252fd989ccf8bb684e82f7f87a98182caf` |

The public rootfs contract passed before launch. The DTB contained exactly four
enabled CPU nodes. QEMU used registered generic Sv39, four harts, 2 GiB RAM,
no network, bochs-display, VirtIO keyboard/tablet, a read-only boot disk, and a
writable copy of the signed Desktop M3 root.

## Command and duration

The gate ran once in
`asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached` with
`--network=none`:

```bash
python3 -m tools.riscv.debian.rootfs.desktop_m3_gate \
  --kernel /work/target/osdk/aster-kernel-osdk-bin.Image \
  --uboot /inputs/target/qemu-uboot/cache/u-boot-build/u-boot \
  --dtb /work/target/qemu-uboot/drm-cursor/prepared/qemu-virt.dtb \
  --stage1-initramfs /inputs/target/debian-riscv/systemd-m2/stage1/initramfs.cpio \
  --root-image /inputs/target/debian-riscv/desktop-m3/rootfs/debian-root.ext2 \
  --root-manifest /inputs/target/debian-riscv/desktop-m3/rootfs/rootfs-manifest.json \
  --packages-lock /inputs/target/debian-riscv/desktop-m3/rootfs/packages.lock \
  --package-checksums /inputs/target/debian-riscv/desktop-m3/rootfs/source-metadata/package-checksums \
  --output-directory /work/target/debian-riscv/desktop-m3/current-main-gate \
  --boot-timeout 300 --command-timeout 30 --cleanup-timeout 10
```

QEMU started at 17:25:56 UTC and the atomic evidence files were published at
17:30:03 UTC, for 247 seconds. Two earlier invocations stopped before QEMU
launch because the output directory was absent and then not root-owned; they
produced no guest run.

## Evidence outputs

| Output | Bytes | SHA-256 |
| --- | ---: | --- |
| `result.json` | 2,252 | `5d20053be3d404746843406ffa9edb87eb1e1eb12f66739f05b0722cead9d0af` |
| `desktop-m3.serial.log` | 66,200 | `798648ecdcde7858e70e9a0437d8a09b1c8747cda055899a73c5879d6a4fd55a` |
| `desktop-m3.ppm` | 3,932,177 | `5a5505bdc91d7023b5fb4584b01735f02da9fedc482763fa5eb45888ff0df733` |
| writable root after the run | 1,073,741,824 | `cf461e72661a763df9992ae971908bc887ee66b86fdffef71559fe5da0bcf2c4` |

The large files remain in the ignored local `target/` tree.

## Boundary

This proves the current-main Asterinas software path through Debian systemd,
udev/logind/PAM, `/dev/fb0`, Xorg fbdev, virtual evdev input, Matchbox, and
xterm in QEMU. It does not prove the physical Megrez framebuffer address,
EIC7700 HDMI/cache behavior, xHCI/USB HID input on this desktop root, native
EIC7700 DRM, accelerated rendering, networking, or a browser.

The next physical step is therefore narrower: use the RAM-only Megrez
simple-framebuffer handoff to replace the latest board log's
`Framebuffer not found` with `Registered firmware framebuffer`, before
installing or booting the Desktop M3 root on the board.
