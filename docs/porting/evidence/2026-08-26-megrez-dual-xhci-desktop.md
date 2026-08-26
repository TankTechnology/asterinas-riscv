# Megrez dual-xHCI Debian desktop evidence

Date: 2026-08-26

Candidate branch: `codex/drm-r1-current-main`

## Result

A physical Milk-V Megrez cold-booted Asterinas into the signed Debian Trixie
Desktop M3 root. Asterinas selected both on-board DWC3/xHCI controllers,
registered a USB boot keyboard and a USB boot mouse, and reached a non-root
Xorg fbdev session with Matchbox and xterm. The operator also confirmed that
the HDMI desktop was visible and basic keyboard input worked.

The physical USB topology was split across two controllers:

| Worker | MMIO | IRQ | Observed device |
| --- | --- | --- | --- |
| USB0 | `0x50480000..0x50490000` | 85 | Logitech `046d:c31c` boot keyboard |
| USB1 | `0x50490000..0x504a0000` | 86 | `30fa:0302` optical boot mouse behind a VIA hub |

The RAM-only U-Boot selector was:

```text
fdt set /chosen asterinas,usb-host \
  /soc/usb0@50480000/dwc3@50480000 \
  /soc/usb1@50490000/dwc3@50490000
```

## Frozen physical inputs

| Input | Identity |
| --- | --- |
| Asterinas Sv39 Image | SHA-256 `a7138e626ee22a7e8779b7fd67105c1b02dc275def8a29a06b35b74bb68e18bb`, 14,073,256 bytes |
| Stage1 initramfs | SHA-256 `27ac53d8c759b99aa67e3c26109e3839dbe5ebd090008eb0a229295c33394e4b`, CRC32 `bd4ac302`, 567,808 bytes |
| Megrez DTB | CRC32 `4afcb20e`, 154,800 bytes |
| Physical serial log | SHA-256 `2ebb6ec284d340afd0e5123daf07661a529bf8748b6c5320503bcf076fa5e204`, 40,447 bytes |

The framebuffer handoff was `0xfd800000`, length `0x7e9000`, 1920x1080,
stride 7680, `x8r8g8b8`. The validation boot used
`asterinas.reboot_after=600`; it was intentionally bounded and did not persist
U-Boot environment changes.

## Ordered physical evidence

The serial transcript contains, in order:

```text
Registered firmware framebuffer: base=0xfd800000 ... resolution=1920x1080
Selected DWC3 USB host 0 ... interrupt=16:85
Selected DWC3 USB host 1 ... interrupt=16:86
Starting DWC3 xHCI host 0 ... irq=16:85
Starting DWC3 xHCI host 1 ... irq=16:86
USB boot mouse registered: 30fa:0302 bus=usb name=usb_boot_mouse
USB boot keyboard registered: 046d:c31c bus=usb name=usb_boot_keyboard
DEBIAN_DESKTOP_M3_INPUT keyboard=evdev pointer=evdev
DEBIAN_DESKTOP_M3_XORG framebuffer=fbdev display=:0
DEBIAN_DESKTOP_M3_CLIENTS window-manager=matchbox terminal=xterm
DEBIAN_DESKTOP_M3_READY user=asterinas display=:0
```

No panic, oops, stopped transfer, or xHCI startup-failure marker appeared in
the final log.

## QEMU regression

The same Image also passed the deterministic QEMU PCI xHCI keyboard-and-mouse
gate with QEMU 10.2.1, Sv39, four harts, 2 GiB RAM, and no network. The gate
recorded all 15 exact evdev records, `passed: true`, and `cleanup: complete`.

| Output | SHA-256 |
| --- | --- |
| Input initramfs | `dc4e90b1300879ca7615c058ff25a28923d8de28b2462c3d9c6d5cdd8f24879c` |
| Serial log | `892acebba99c1e560d125470fa91851f7f6d4a9145cdd52109d4da2f23d86e11` |
| Result JSON | `3d2663c01f4be5b60c8e2ac7497a0d406e5b5b34ed1d2db1aba621bd8b0e6e86` |

## Boundary and next step

This proves cold-boot discovery on both physical controllers, interrupt-driven
USB HID workers, evdev registration, and selection by the physical Debian/Xorg
desktop. QEMU additionally proves exact relative motion and left-button event
delivery. The physical serial log cannot prove that a human moved and clicked
the pointer, so that final HDMI interaction remains an operator check.

This milestone does not yet prove hotplug after boot, arbitrary HID report
protocol devices, accelerated DRM rendering, Asterinas networking, a browser,
or a persistent desktop boot without the protection timer.
