# Debian Desktop M4 applications on QEMU and Megrez

Date: 2026-08-26

Candidate branch: `codex/drm-r1-current-main`

Candidate source commit before this evidence page: `f3d9c73fc`

## Result

The signed Debian Trixie `riscv64` Desktop M4 profile reached its complete
ordered gate on both QEMU and a physical Milk-V Megrez running Asterinas. The
non-root Xorg fbdev session contained Matchbox, PCManFM, NetSurf, and xterm.
The physical boot also registered the real USB keyboard and mouse through the
two on-board DWC3/xHCI controllers and selected both devices through evdev.

The physical result was established twice:

1. a bounded boot emitted every M4 marker, then the 180-second Asterinas safety
   timer cold-rebooted the board and the host stopped it at the next U-Boot
   prompt with `ready_seen=True`;
2. a second boot used the same frozen inputs but omitted
   `asterinas.reboot_after`. It emitted the same complete marker sequence and
   was left running as the persistent HDMI desktop.

Neither boot changed the persistent U-Boot environment.

## Frozen inputs

| Input | Identity |
| --- | --- |
| current-main Sv39 Asterinas Image | SHA-256 `a7138e626ee22a7e8779b7fd67105c1b02dc275def8a29a06b35b74bb68e18bb`, CRC32 `99058d25`, 14,073,256 bytes |
| Megrez DTB | CRC32 `4afcb20e`, 154,800 bytes |
| Desktop M4 Stage1 initramfs | SHA-256 `f809de13b3d78ddcbedf297b4b4c0024d76c3f8adca16f65a24d3b79b3e23538`, CRC32 `e7e11bf8`, 567,808 bytes |
| Desktop M4 ext2 | SHA-256 `980c4bf66643a1bb5a2b5f94016ff72168cbff2f45028e0bb068a30a5c6d0eb3`, 1,073,741,824 bytes |
| Desktop M4 manifest | SHA-256 `bbc17cd8f5f51381ee06571989fd5579a51ede7aee0fe8c82477951fc93da211` |
| Desktop M4 packages lock | SHA-256 `00af54095fb4f1f2ff4736e3d9f382a2211e2cd6019a68478878c1b6983d22dd` |
| package checksums | SHA-256 `7edb48185bf383582f6b31fd3998caae8ff8b9c5eb8b6e92b5cf7bc2816e40c2` |
| automatic-reboot installer | SHA-256 `4dc5f9c00b4989531a335d6eb51f4ce8cd122a1758d487cdce094024269f542c`, CRC32 `3f5b41f3`, 277,467,648 bytes |

The root contract identifies Debian 13.6, `riscv64`, ext2, 4 KiB blocks,
label `ASTER_DEBIANM4`, UUID `e13bd1e8-8719-539f-b5e7-5c7b5f5df3c8`, and
282 downloaded package records.

## Asterinas installation evidence

RockOS was used only to copy the already frozen boot artifacts over the local
1 Gb/s Ethernet link. Asterinas, not Linux, wrote the Debian image to eMMC
partition 2. The resumed installer verified every 32 MiB chunk, read back the
full 1 GiB partition, and emitted:

```text
DEBIAN_INSTALL_PASS sha256=980c4bf66643a1bb5a2b5f94016ff72168cbff2f45028e0bb068a30a5c6d0eb3 bytes=1073741824
```

It then executed `sync` and `reboot -f`; Asterinas requested an SBI cold
reboot and returned to a new OpenSBI/U-Boot epoch. The application root was
therefore not installed by bypassing Asterinas.

## Ordered physical evidence

U-Boot loaded and CRC-checked the Image, DTB, and Stage1, then added a RAM-only
1920x1080 `simple-framebuffer` and selected both DWC3 hosts. The persistent
boot used:

```text
console=ttyS0 loglevel=info init=/init asterinas.mmc_write_partition2 -- --root-init=systemd
```

The serial transcript contains the following hardware and desktop boundaries:

```text
Registered firmware framebuffer: base=0xfd800000 ... resolution=1920x1080
Selected DWC3 USB host 0 ... interrupt=16:85
Selected DWC3 USB host 1 ... interrupt=16:86
USB boot mouse registered: 30fa:0302 bus=usb name=usb_boot_mouse
USB boot keyboard registered: 046d:c31c bus=usb name=usb_boot_keyboard
DEBIAN_DESKTOP_M4_UDEV state=active
DEBIAN_DESKTOP_M4_LOGIND state=active
DEBIAN_DESKTOP_M4_SESSION user=asterinas tty=tty1
DEBIAN_DESKTOP_M4_INPUT keyboard=evdev pointer=evdev
DEBIAN_DESKTOP_M4_XORG framebuffer=fbdev display=:0
DEBIAN_DESKTOP_M4_CLIENTS window-manager=matchbox file-manager=pcmanfm browser=netsurf terminal=xterm
DEBIAN_DESKTOP_M4_READY user=asterinas display=:0
```

The bounded and persistent logs contain no M4 failure marker, panic, or oops.
The persistent serial reader exited after READY and released `/dev/ttyUSB0`;
closing that host reader does not stop the board or the graphical session.

## QEMU regression and screenshot

The same Image, Stage1, signed root, manifest, lock, and package checksums also
passed the Desktop M4 QEMU gate with registered generic Sv39, four harts,
2 GiB RAM, no network, bochs-display, VirtIO keyboard/tablet, and a writable
copy of the root. `result.json` records `passed: true`, 1280x1024, 256 sampled
colors, and 1,308,128 non-background pixels. The screenshot visibly contains
PCManFM, NetSurf, xterm, and the pointer.

| QEMU output | Bytes | SHA-256 |
| --- | ---: | --- |
| `result.json` | 2,260 | `f586f46d7ff8205296eb92a2a272f6afc4a220db2a46ba0e3e87265e45517d90` |
| `desktop-m4.serial.log` | 47,114 | `0bcc1175f2850f4e675285b734d9f922f2ceb0201e0b7f9ceba0bc8ea3453bd1` |
| `desktop-m4-qemu.png` | 22,616 | `0e0b8acb790f962b5ad78481aa0941b5a38a28a752f797e74c3dad6d03300251` |
| writable root after the run | 1,073,741,824 | `35542bb4791705cfc44edfb971e3c93642b9fa61b6ad25f86d08a919a195db65` |

The QEMU input gate proves deterministic pointer motion and left-button event
delivery. The physical transcript proves that the mouse is registered and
selected by Xorg; human observation of pointer movement on HDMI remains a
separate operator check.

## Physical log identities

| Log | Bytes | SHA-256 |
| --- | ---: | --- |
| initial installer | 21,220 | `14b0d3f22eb379624378c948305cb6f254018c5dfab06a97ab7f204dd4a43cec` |
| resumed installer and final PASS | 23,180 | `ee94ab92489b0d8910954b05686cd195916c8ebf925ffbad0bf09774ed13ae7c` |
| bounded serial-console desktop | 45,294 | `453191171dc2cc14b32787d02ddb9d02241fe01caed78f5416df057d0cae5f44` |
| persistent desktop | 42,620 | `44eebdbb50b908e7c1128e19bb138c6d220166fbf7575c2b69e77a80c49d1b8a` |

The raw logs and large artifacts remain in the ignored local `target/` tree.

## Boundary and next step

This milestone proves the basic Asterinas desktop application path on Megrez:
signed Debian userspace, systemd/udev/logind, firmware framebuffer, Xorg fbdev,
two physical xHCI controllers, USB keyboard and mouse through evdev, Matchbox,
PCManFM, NetSurf, and xterm. It also proves that serial debugging can remain
available while the HDMI graphical session runs.

It does not yet prove Asterinas networking, browser network access, JavaScript,
audio, USB hotplug after boot, arbitrary HID report devices, native EIC7700
DRM, acceleration, or desktop performance. The next foundation milestone is a
bounded physical mouse-interaction check and then a native network interface;
DRM acceleration and a larger desktop environment remain later work.
