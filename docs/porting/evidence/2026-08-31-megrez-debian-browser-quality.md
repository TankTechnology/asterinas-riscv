# Megrez Debian browser-quality rootfs and acceptance

Date: 2026-08-31

Candidate branch: `codex/megrez-dwmac-board-current`

Source commits used by this run: `c57f3c4b9` and `6b7ffa04c`. Both commits are
already pushed to `origin/codex/megrez-dwmac-board-current`.

## Rootfs installation

The frozen Debian 13.6 `riscv64` browser-quality image was installed into
Megrez eMMC partition 2 through the Asterinas installer. No Linux runtime was
used to write or validate the target partition.

| Artifact | Identity |
| --- | --- |
| root image | 1 GiB, SHA-256 `aa72723ed16b8fab400299f3ec8dda241360d24285d13c1884d3de4d78afcca7` |
| install plan | `daa8b092450870eec3ffd79aadce1fdee9bc464d522ba029dc2b928b4c07ec68` |
| installer result | `install-pass`, plan-bound, with installer serial evidence |

The installer resumed already-correct ranges, verified every transferred range,
read back the complete image, and emitted:

```text
DEBIAN_INSTALL_PASS sha256=aa72723ed16b8fab400299f3ec8dda241360d24285d13c1884d3de4d78afcca7 bytes=1073741824
```

The result and raw transcript are retained locally under:

```text
target/megrez-debug/browser-quality-r900-f526d8a62/install-6b7f/result.json
target/megrez-debug/browser-quality-r900-f526d8a62/install-6b7f/installer.serial.log
```

## QEMU quality gate

A second plan reused the same immutable artifacts and changed only the
automatic recovery interval from 600 to 1200 seconds. The QEMU Desktop M8
gate passed with SMP=4 and Sv39:

```text
plan_sha256=ae2cd106e0bdd703e8e0dcc3a3ecafa73cd2bcbc86ee8ef51a8dcf7aa72c9ec3
DEBIAN_BROWSER_M6_READY remote=baidu javascript=limited-pass
DEBIAN_BROWSER_M7_READY page=baidu capture=pending
DEBIAN_BROWSER_M8_FIXTURE text=cjk-latin image=png form=query
DEBIAN_BROWSER_M8_SCROLL direction=end-home
DEBIAN_BROWSER_M8_NAVIGATION second=loaded back=loaded forward=loaded
DEBIAN_BROWSER_M8_DOWNLOAD bytes=262144 sha256=2312394bd99545d9de131c24efb781e765ac1aec243f2ed9347597a793a415e9
DEBIAN_BROWSER_M8_SOAK seconds=120 process=alive
DEBIAN_BROWSER_M8_CAPTURE bytes=163995 sha256=5d25d451948b2299b1dfe50f80bb347b630b2c41b60b7eeccc285991f82160f3
DEBIAN_BROWSER_M8_READY quality=lightweight
```

The final simulated desktop image is [available locally](../../../target/megrez-debug/browser-quality-r1200-f526d8a62/qemu-desktop-m8-final.png)
when the ignored `target/` artifacts are present. It is a QEMU screenshot,
not a raw HDMI capture from Megrez.

## Megrez physical boundary

The physical run reused the installed partition and did not reflash the rootfs.
The following boundaries passed:

- GMAC1 selected the board RJ45 link at 1 Gbps; the 20-request 64 KiB stress,
  HTTP-date clock, HTTPS `www.baidu.com` status 200, and Baidu PNG checks passed.
- GMAC diagnostics reported `fifo_overflow_packets=0`, `read_failures=0`, and
  `rx_buffer_unavailable=0` during the run.
- Both DWC3/xHCI hosts initialized; the Logitech USB keyboard was registered
  through HID/evdev and remained usable.
- NetSurf opened `https://m.baidu.com/`, identified the Baidu title, and loaded
  an `asterinas` search result.
- The firmware framebuffer was handed off at 1920x1080 and Xorg used the
  `FBDEV` driver; the desktop service exposed PCManFM, Openbox, xterm, and
  NetSurf windows.

The board did not have a physical mouse connected. M4 therefore emitted the
documented pointer-degradation diagnostic and did not claim a physical pointer
pass. M8 uses pointer clicks for its form, link, and download actions; no M8
marker was emitted before the bounded host run ended with
`reason=guest-timeout`. The serial evidence contains no kernel panic or GMAC
loss indication. The first 600-second run did observe the automatic return to a
fresh U-Boot epoch, while the second 1200-second run was stopped by the host
gate before a separate recovery observation.

The physical result is retained locally at:

```text
target/megrez-debug/browser-quality-r1200-f526d8a62/physical-6b7f/result.json
target/megrez-debug/browser-quality-r1200-f526d8a62/physical-6b7f/serial.log
```

## Interpretation and next gate

This milestone establishes a usable Debian rootfs, wired browser networking,
Chinese/raster page rendering, keyboard input, and the complete lightweight
NetSurf quality flow in QEMU. It does not establish modern JavaScript parity,
Firefox/Chromium compatibility, physical mouse interaction, DRM acceleration,
or a raw HDMI screenshot path.

The next physical run should not rewrite the partition. Once a USB mouse is
present, rerun only the plan-bound browser-quality board gate to complete M8.
Firefox should be evaluated as a separate profile: first prove that Debian's
`firefox-esr` riscv64 binary starts with the existing Xorg/fbdev/input stack,
then add a loopback Marionette content gate, and only afterward investigate
network/TLS and rendering performance. A Firefox result must not be inferred
from the current NetSurf pass.
