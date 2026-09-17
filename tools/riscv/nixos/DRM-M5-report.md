# DRM-M5 — full-system integration verification (DRM + ALSA + NetSurf)

Date: 2026-08-15
Branch: `track/drm`
Status: **PASS** — one boot runs the systemd desktop (Xorg *modesetting* on
`/dev/dri/card0`) **and** plays a 440 Hz tone through virtio-sound (ALSA)
**and** renders the NetSurf home page, all on the merged kernel.

---

## 1. Summary

DRM-M5 is the integration milestone after the DRM line wrapped (PR #41). Its
goal is not new kernel feature work but proving that the three previously
independent kernel results — **DRM/KMS**, **virtio-sound + ALSA**, and
**`clock_getres`** — coexist in one tree and one boot, and fixing whatever the
combination exposes.

The work splits into four pieces:

1. **Merge** `origin/main` (DRM + `clock_getres`, PR #40/#41) into `track/drm`.
2. **Merge** `origin/virtio-sound-driver` (virtio-sound + ALSA PCM ABI, PR
   #35/#42) on top — `origin/main` does **not** yet carry ALSA.
3. **Build** the merged kernel and re-run the DRM-M4 cursor regression harness.
4. **Boot once** with `virtio-gpu` + `virtio-sound` + `virtio-net` + the
   systemd desktop, and verify all three subsystems together.

One real integration bug was exposed and fixed (§5): the minimal keyring stub
returned `EOPNOTSUPP` for `KEYCTL_SETPERM`/`LINK`, which systemd 257 treats as a
hard failure, so **every** service (Xorg, NetSurf, the ALSA test, …) died at
spawn with `status=237/KEYRING`.

## 2. Merge state

`track/drm` now contains, in order:

| commit | what |
|---|---|
| `0321f4661` | merge `origin/main` (DRM PR #41 + `clock_getres` PR #40) |
| `160f7fed0` | merge `origin/virtio-sound-driver` (ALSA PR #35/#42) |
| `905491522` | fix: keyctl `SETPERM`/`LINK`/`UNLINK` succeed (systemd desktop spawn) |

The two merges are clean apart from three `add/add` conflicts in the DRM files
(`gpu/device.rs`, `gpu/mod.rs`, `device/dri.rs`). Those were byte-identical
cherry-picks where `track/drm`'s copy is a strict superset (it additionally
carries the M4 hardware-cursor work), so they resolve to "take ours" with zero
lost lines. The virtio device registry (`device/mod.rs`), `lib.rs` dispatch, and
the kernel `device/mod.rs` all correctly register both `Gpu = 16` and
`Sound = 25` plus `dri` and `snd` device init.

## 3. What was verified (one boot, three subsystems)

The guest is the sibling `asterinas-riscv` tree's systemd desktop (Xorg +
matchbox-wm + xpanel + pcmanfm + xterm + NetSurf GTK, cross-compiled for
riscv64), layered with the DRM modesetting driver + libdrm and the Alpine musl
`aplay`/`alsa-lib` userspace. QEMU runs `virtio-gpu` (DRM), `virtio-sound` with a
`wav` backend (ALSA), `virtio-keyboard`/`virtio-tablet` (input), and `virtio-net`
(optional). See `tools/riscv/nixos/m5/{build_m5.sh,boot_m5.py,alsa_test.c,alsa.service}`.

### 3.1 DRM / desktop (Xorg modesetting)

Xorg loads `modesetting_drv.so` and drives `/dev/dri/card0` (not the bochs
`fbdev` framebuffer used by the earlier desktop milestones):

```
(II) Loading /usr/lib/xorg/modules/drivers/modesetting_drv.so
(II) modeset(0): using default device
(**) modeset(0): Option "ShadowFB" "true"
(II) modeset(0): Output Virtual-1 connected
(II) modeset(0): Output Virtual-1 using initial mode 1280x800 +0+0
```

systemd reaches `graphical.target`; `xorg.service`, `matchbox-window-manager`,
`xpanel`, `pcmanfm`, `xterm`, and `netsurf` all start. The guest marker set is
all `OK`:

```
init-launcher OK · graphical-target OK · xorg-started OK · xorg-banner OK
xorg-input-devices OK · netsurf-started OK · alsa-done OK · alsa-pass OK
```

### 3.2 ALSA

The `alsa.service` oneshot runs `aplay -D hw:0,0 /sine.wav` (440 Hz / 48 kHz /
S16LE / stereo) against `/dev/snd/pcmC0D0p`, and the host decodes QEMU's `wav`
backend output:

```
=== ALSA audible-tone verification ===
  fmt       : 2 ch, 48000 Hz, 16-bit, 48128 frames
  amplitude : RMS=11568.7  peak=16383 (min RMS 2000)
  pitch     : 438.8 Hz (expect 440 +/- 12)
  audible   : OK
```

`__ALSA_EXIT=0__ __ALSA_DONE__ __ALSA_PASS__` — the PCM really left the guest
(amplitude + pitch, not just a byte count).

### 3.3 NetSurf

`netsurf.service` starts NetSurf 3.9 on the bundled local home page
(`file:///usr/share/netsurf/netsurf-home.html`). The post-settle screendump is
no longer solid black — the page body and chrome have painted:

```
=== screenshot histogram (1280x800) ===
  colour (160,160,160): 1728   (UI chrome / cards)
  colour (0,0,0)      :  995   (root window)
  colour (224,224,224):  926   (white cards / toolbar)
  colour (32,32,32)   :  197   (panel)
  colour (192,192,192):  179
  non-black: 75.7%   cream(#f4e8d0): 188   blue(#1a4f8b): 2
```

The home page's body `#f4e8d0` and heading `#1a4f8b` colours are present, i.e.
the page actually rendered (the earlier `--settle-seconds 20` run showed a
solid-black screen because NetSurf's UI-resource load is slow under TCG; the
final run uses a 120 s settle).

## 4. Regression check

Before the full boot, the merged kernel was re-run through the DRM-M4 cursor
harness (`boot_drm_m4.py`) to confirm the DRM merge didn't regress the hardware
cursor: `__DRM_PASS__`, all three cursor ioctls accepted, 4
`virtio_gpu_update_cursor` trace events.

## 5. Integration issue found & fixed

**Symptom.** On the first full boot, `xorg.service`, `netsurf.service`,
`matchbox-window-manager`, `pcmanfm`, `xpanel`, `xterm`, *and* `alsa.service` all
failed identically:

```
xorg.service: Failed at step KEYRING spawning /usr/bin/Xorg: Operation not supported
xorg.service: Main process exited, code=exited, status=237/KEYRING
```

**Root cause.** systemd 257's per-service `setup_keyring()` (in
`exec-invoke.c`) joins a session keyring and then:

- `keyctl(KEYCTL_LINK, …)` (shared-keyring mode), and
- `keyctl(KEYCTL_SETPERM, key, …)` after `add_key("user","invocation_id", …)`.

Both are **hard failures** on any error other than `ENOSYS`. The kernel's
keyring stub (`kernel/src/syscall/keyctl.rs`, from PR #33) only handled
`GET_KEYRING_ID` / `JOIN_SESSION_KEYRING` / `REVOKE` and returned `EOPNOTSUPP`
for everything else — so systemd refused to spawn the service. This was never
seen before because the desktop was only ever booted on the sibling
`asterinas-riscv` tree's kernel, which does not implement keyctl at all
(`ENOSYS` → systemd skips the keyring gracefully).

**Fix.** `905491522` makes `KEYCTL_SETPERM` (5), `KEYCTL_LINK` (8) and
`KEYCTL_UNLINK` (9) no-op successes returning 0. Key storage is a stub that
never retains keys, so linking/unlinking/setting-permission are trivially
"already done"; returning success is what lets systemd proceed.

## 6. Remaining gaps (non-fatal, observed this run)

| gap | severity | notes |
|---|---|---|
| `modeset(0): failed to get plane resources: Inappropriate ioctl for device` | non-fatal | `DRM_MODE_PLANE_GETRESOURCES` isn't implemented; Xorg falls back to ShadowFB (the path we force anyway). |
| `WARN: framebuffer: Framebuffer not found` | expected | `virtio-gpu` has no simple-framebuffer DTB node, so there is no `/dev/fb0`; the desktop drives DRM instead. |
| NetSurf favicon / link fetches: `cURL code 6` (Could not resolve hostname) | expected | no network device attached in the default run; `--net` is available but the local home page is the deterministic render check. |
| `Unimplemented syscall number: 170/258/264/280/285/293` warnings | pre-existing | systemd works around them (`memory.max` cgroup, `fs.nr_open`, timerfd flags, …). |
| `Nix profile activation` service fails | non-critical | unrelated Nix smoke unit; no effect on the desktop/ALSA/NetSurf path. |

## 7. Harness

`tools/riscv/nixos/m5/`:

| file | purpose |
|---|---|
| `build_m5.sh` | assemble the integration initramfs (desktop + modesetting driver + libdrm + musl aplay/alsa-lib + `alsa.service`) and re-pack `/tmp/drm-m5/boot.ext4` with the merged kernel |
| `boot_m5.py` | boot with `virtio-gpu` + `virtio-sound` + `virtio-net`; verify systemd desktop markers, decode the host WAV, and histogram the screendump |
| `alsa_test.c` | static launcher that forks+execs `aplay` and prints `__ALSA_*` markers |
| `alsa.service` | oneshot `Before=graphical.target`, output to `/dev/ttyS0` |

Run: `bash tools/riscv/nixos/m5/build_m5.sh && python3 tools/riscv/nixos/m5/boot_m5.py`.

## 8. Result

```
=== DRM-M5: PASS (smp=1) ===
```

All three kernel results — DRM/KMS, virtio-sound + ALSA, and `clock_getres` —
coexist in `track/drm`, and a single boot runs the full systemd desktop
(Xorg modesetting on DRM), plays audible sound through virtio-sound, and renders
a page in NetSurf.
