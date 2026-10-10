# Megrez fb0 desktop watchdog handoff (2026-10-11)

This record covers one guarded physical desktop cycle after the QEMU gate passed.
It does not claim HDMI pixels were captured; no HDMI capture device was attached.
The physical evidence is the kernel fb0 transaction, serial markers, and recovery
attestation.

## Inputs and gates

- QEMU receipt: `target/qemu-desktop-m4-watchdog-current-20261011/result.json`
  (`passed=true`, profile `desktop-m4`, 1280x1024 screenshot, 86 sampled colors,
  application paint verified, kernel `8a1c81414d7875845925db924affe863960805d17782e8a150db46287f8912cd`,
  stage1 `7806b8348eb10456930388bdd250880205d198035b2da5cf7be17348a2a5f671`).
- Physical candidate: `target/megrez-physical-step1/desktop-m4-watchdog-handoff-20261011/`.
  Its menu digest is `56512272f6cea77a8065e2a67d68a1a3b9e40aefe1bbb4cbe41fa3e04df78155`.
- The rootfs identity used by QEMU and the board's first 1 GiB rootfs window is
  `e8fdc1abb08d6a528a59c847873a9f424a5eab1551f982e3b9a9122cae098a20`; the
  read-only partition attestation is in
  `target/current-main-physical-graphics/physical/desktop-m4-current-partition-attestation-20261011/receipt.json`.
- Publication was configuration-last and recovered to RockOS:
  `target/megrez-physical-step1/desktop-m4-watchdog-handoff-20261011/publication/`.

## Physical result

The guarded run is recorded in
`target/current-main-physical-graphics/physical/display-20261011-watchdog-handoff-r2/`.
It passed with `boot_to_ready_seconds=63.2819` and `menu_to_ready_seconds=60.6778`.
The serial transcript contains:

- `ASTERINAS_HARDWARE_WATCHDOG_HANDOFF status=active period_seconds=1`;
- `ASTERINAS_DEBUG_CONSOLE_READY uid=0`;
- `DEBIAN_DESKTOP_M4_FB_RW width=1920 height=1080 stride=7680 bpp=32 format=BgrReserved rect=16x16 status=pass`;
- `DESKTOP_READY:<nonce>`;
- one `systemctl --force reboot` followed by a fresh OpenSBI/U-Boot epoch.

The runner closed and reopened the serial device before authenticating recovery.
The reopened root command reported UID 0 and boot-id
`22395bab-bc29-406d-9140-28718f97791b`; an independent SSH read-only check saw
RockOS as UID 1000 on `/dev/mmcblk1p3` with the same boot-id.

An earlier attempt stopped before guest loading because the candidate menu had
not yet been published; watchdog recovery was observed and that attempt is not
counted as a display result.
