# Downstream pick ledger (2026-10-31)

## Base

- Upstream: `upstream/main` = `cdd5bc10907f752dc19a399297ea1fd57af20ee4`
- Rebase branch: `rebase/upstream-20261029` = `feature/ext4-jbd2`
- Source product branch: `feature/qemu-network-runtime` = `af48751df`
- Scope: QEMU-supported Debian/RISC-V surface. Board/GPU/DRM work remains out
  of scope unless separately approved.
- Current stack: 59 commits over upstream, including this P0 desktop-evidence
  change.

This ledger records the provenance of the current stack and the dependency
closure for the still-missing desktop/browser automation. It is updated in the
same change as each new pick. See
`.agents/skills/upstream-rebase-cherry-pick/SKILL.md` for the procedure.

## Delivered on the rebase line

| Feature | Representative source commit(s) | Current commit(s) | Action | Evidence |
|---|---|---|---|---|
| virtio-net 10-byte header | `ce2cf1662` | `5cf47ed96` | semantic port | `direct`/network gates |
| unprivileged port-zero connect | old product tree | `56218c47c` | new on rebase line | ktest + dsh |
| SCM_RIGHTS UNIX-socket passing | `79ee67872`,`f901a9f0f` | `f3830f7cb`,`d92b6530a` | semantic port + test | Firefox content process |
| klog, `/dev/kmsg`, boot identity, `syslog(2)` | old klog work | `415c233e4`,`978d0413b` | port/new | journald, `/dev/kmsg` probes |
| devtmpfs, `/dev/shm`, devpts/PTY | `3604162f8` and related | `977757632`,`2f42172df` | port | PTY probes |
| framebuffer graphics class + DT simple-framebuffer | `0ce8ab960` and related | `bd966a253`,`6cea7ade3` | port | desktop framebuffer evidence |
| rtnetlink set, IPv4 routes, `/proc/net/{dev,route,tcp,udp}`, netns | `ef469eb48`,`779667a03` | `fb837cf21`,`f5febeb7a`,`e9375313c`,`b32616377`,`980e47646` | port | QEMU network/procfs evidence |
| Debian rootfs/Stage1/direct gate/Image sidecar | old builders and gates | `fc78d33a8`,`25909f303`,`0881283db` | port/adapt | rootfs contract, direct gate |
| ext4 extents + JBD2 + crash gates | `294c225ce` | `30cd05424`,`7b2bfc87d`,`7c960b105` | superseded/expanded | 64-bit and crash gates |
| ext2 durability invariants | old ext2 work | `31acb7c3c` | port/new | ktests + ext2/e2fsck probe |
| host suite + aggregate product gate | old host suite, not cherry-picked wholesale | `2abf63a3a` | new on rebase line | 90 host tests, product receipt |
| Desktop direct-boot gate (kernel + Stage1 initrd + RAM-fb DTB, ordered session markers, visible Firefox, guest framebuffer pixels) | new on rebase line; old gate behavior used as reference | this P0 change | new + consolidated | `test_desktop_gate.py`, `desktop_frame_evidence.sh`, fb5 `result.json`, `firefox.png` |

`superseded` means the old implementation exists, but the current line's
implementation is the one to keep. Do not replace current modules with the old
copies merely for provenance.

## Desktop consolidation (2026-10-31)

A direct QEMU boot using `-kernel Image -initrd <full Stage1 cpio> -dtb
fb-bochs.dtb` reached systemd, Xorg, the window manager, the desktop session,
and `panel-start` on the current `browser-web` root. The compact
`tools/riscv/debian/desktop_gate.py` now checks those ordered markers and
passes with a real QEMU run. U-Boot and the historical M3/M4 gate stack are not
needed for this check.

As a result, the previously picked scaffolding was removed:

- `qemu_qmp.py`, `qemu_ppm.py` (`961b7ceb7`) — removed;
- `gate_protocol.py`, `gate_runtime.py`, `rootfs_gate.py`,
  `rootfs_gate_backend.py`, `systemd_m2_gate.py` (`6b62ca317`) — removed;
- `desktop_m3_gate.py`, `desktop_m4_gate.py` (`9d597171c`) — removed;
- their wrapper test modules — removed.

Reasons:

- The generic framework is U-Boot/two-boot shaped and much larger than the
  direct-boot desktop check.
- QMP screendump of the bochs display returned a 640x480 VGA image even with
  an explicit display device, while the guest consumes the 1280x1024
  simple-framebuffer. It is not reliable pixel evidence for this boot path.

## P0 desktop evidence (2026-10-31)

The marker-only gate was subsequently found to pass on a dead framebuffer.
Direct boot has no firmware to program the bochs PCI BAR: `info pci` showed
BAR0 at `0x0`, and `pmemsave 0x40000000` returned all `0xffffffff`. The gate
therefore did not prove pixels.

P0 keeps direct boot and extends the same `desktop_gate.py`:

- `make_fb_ram_dtb.sh` moves the simple-framebuffer to the top of QEMU RAM
  (`0xf0000000` for the 2 GiB contract) and shrinks the memory node.
- The browser-web rootfs installs `asterinas-desktop-frame-evidence.service`
  from `desktop_frame_evidence.sh`; it waits for Xorg, settles the desktop,
  finds a visible, geometry-checked Firefox window (`xdotool`, `xwininfo`
  fallback), and emits the Firefox/frame-ready markers.
- The gate passes `ASTERINAS_BROWSER_WEB_BASIC_ONLY=1` and
  `ASTERINAS_WEB_NETWORK_MODE=direct`, captures desktop and Firefox frames
  from guest RAM through the QEMU monitor, validates non-black/colour-rich
  pixels, and requires the Firefox window to change the frame.

The first real pass is
`evidence/2026-10-31-p0-desktop-gate-fb5/`: `status=pass`,
`changed_fraction=0.993874`, and a rendered Firefox New Tab window. The
failed diagnostic runs are preserved under
`2026-10-31-p0-fbprobe-ram/`, `2026-10-31-p0-desktop-gate-fb4/`, and
`2026-10-31-p0-firefox-diag/`. The skill's pitfall table records the dead
BAR, the missing network mode, the `read`/newline bug, and the helper-window
geometry filter.

## Decisions

- **Skip** board/GPU/DRM/Megrez commits from the old tree; not part of this
  QEMU workstream.
- **Skip** the old ext4 implementation as a pick source; the current
  `30cd05424` plus `31acb7c3c` stack is the maintained implementation.
- **Do not** merge old branch history; the old and new trees have different
  paths and contracts.
- **Do not** pick a gate before its closure; clean textual application is not
  a runnable feature.

## Next action

The desktop Firefox/framebuffer gate is delivered. Next candidates, in
priority order:

1. xfstests / broader ext4 coverage (P1);
2. IPv6 route netlink and route deletion;
3. dsh service/launcher integration.

Every new source-based change still gets a `Picked-from:` or `Ported-from:`
trailer and a row here.
