# QEMU desktop-m4 scanout investigation (2026-10-07)

An initial relaxed QEMU profile passed the guest framebuffer ABI gate, but the
stricter scanout-quality gate remains intentionally blocked. The
run used the current RISC-V kernel, U-Boot, DTB, stage-1 initramfs, and the
`desktop-m4-explicit7` root image. The complete artifact is retained under
`target/qemu-desktop-m4-final6/`.

The desktop protocol reaches its udev, logind, session, Xorg/fbdev, shell,
client, and ready markers. In the relaxed run the guest probe reported:

```
DEBIAN_DESKTOP_M4_FB_IOCTL width=1280 height=1024 stride=5120 bpp=32 format=BgrReserved
DEBIAN_DESKTOP_M4_FB_SAMPLE bytes=4096 sha256=ad7facb2586fc6e966c004d7d1d16b024f5805ff7cb47c7a85dabd8b48892ca7
DEBIAN_DESKTOP_M4_FB_COLORS distinct=3 non_background=2
```

The probe uses a bounded sparse sample in the ordered QEMU profile. Physical
and offline profiles retain the complete-frame read and PNG path. The relaxed
sample saw only two non-background points, which is insufficient evidence of
a painted desktop, so the gate now requires at least sixteen sampled points.
The QEMU configuration also disables fbdev ShadowFB to force direct scanout.

The latest gate result is `passed: false` with
`reason: "guest reported desktop failure"`; the guest retries the probe until
the bounded deadline and still reports a single color or insufficient content.
The latest input identities and failure transcript are recorded in
`target/qemu-desktop-m4-final6/result.json` and
`target/qemu-desktop-m4-final6/desktop-m4.serial.log`.

The result proves that the QEMU fbdev ABI and desktop process lifecycle work,
and that a tiny number of pixels can change. It does not yet prove normal
desktop scanout, a physical PowerVR/DRM path, a second QEMU boot, or a physical
desktop boot. The physical launch remains frozen until the stricter QEMU gate
passes.

## Host-side scanout check

To separate guest fbdev reads from the actual QEMU display, a bounded diagnostic
boot issued HMP `screendump` after systemd reached the desktop startup window.
The resulting P6 image was `1280x1024` (3,932,160 RGB bytes), with
`distinct=1`, `non_background=0`, and first pixel `000000`. This means the
guest-side three-color sample was not evidence that the bochs scanout changed;
the host-visible display remains black. The diagnostic image is intentionally
not used as a passing artifact, and the temporary direct-write script was
removed from the candidate image afterward.
