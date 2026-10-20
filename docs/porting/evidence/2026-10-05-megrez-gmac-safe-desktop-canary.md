# Megrez GMAC-safe desktop canary

Date: 2026-10-05 (Asia/Shanghai)

This run used the immutable generation `990f41aeec2de0ac` and the
`minimal` volatile U-Boot device profile. U-Boot disabled both GMAC nodes and
both DWC3 child nodes in the in-memory DTB only; no environment or MMC boot
data was changed.

## Observed boot boundary

The Asterinas kernel entered at 1.26 s, reached root handoff at 13.52 s, and
reported `ASTERINAS_DEBUG_CONSOLE_READY uid=0` at 35.13 s. Its own logs show:

* `aster-dwmac: EIC7700 platform initialization failed: InvalidDeviceTree`;
* both selected USB hosts rejected with `Disabled`;
* framebuffer registration completed before userspace.

This is the expected fail-closed result for the minimal profile. There was no
GMAC-1 register probe, clock/reset programming, PHY access, or DWC3 transfer.

## Result

The canary did not reach `ASTERINAS_DESKTOP_DISPLAY_READY`. The userspace
desktop gate stopped at `browser-start` because this profile intentionally has
no network and no USB input devices, while the existing browser desktop gate
requires both. The failure is recorded in
`target/megrez-desktop-boot/start-20261005-gmac-safe/result.json` and the full
serial transcript is in `serial.log` beside it.

Asterinas performed its synchronized software recovery. The default firmware
path booted RockOS directly instead of stopping at the U-Boot prompt. A fresh
SSH response authenticated UID 0 and reported boot-id
`4929d24e-63f8-4f9b-980c-6b760a8e36bf` on `rockos-eswin`. The old recovery
observer incorrectly called this a U-Boot timeout; the desktop boot tool now
records a direct RockOS boundary separately from an interactive U-Boot
boundary.

The follow-up serial-only login attempt did not see a login prompt within its
bounded deadline. Serial reconnection is therefore unverified in this receipt;
the board was left alone after the authenticated SSH check, with no further
reboot or probing attempt.

## Follow-up gate

Before another physical boot, QEMU must exercise a display-only profile that
does not require USB or Firefox. The physical sequence then remains one change
per boot: display-only, display plus USB, and only then a network/Firefox
profile after a separately audited GMAC selection. No GPU or DRM acceleration
claim follows from this canary; it used the firmware framebuffer/software
rendering path.
