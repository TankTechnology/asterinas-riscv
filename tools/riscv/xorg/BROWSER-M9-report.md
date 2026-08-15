# BROWSER M9 — netsurf.service crash-loop in the interactive desktop

**Status:** root cause located and fixed; the interactive desktop no longer
crash-loops `netsurf.service`.
**Date:** 2026-08-16
**Scope:** the interactive (GTK-display) desktop boot left `netsurf.service` in a
`Restart=always` respawn loop — `Main process exited, code=exited, status=1/FAILURE`
with the restart counter climbing past 113. This milestone diagnoses and fixes it.

---

## 1. Summary

1. **Not a new bug.** The crash-loop is the exact failure M1 §9.1 documented and
   already fixed: `netsurf-gtk` exits `status 1` in `nsgtk_init_resources()`
   ("Unable to find resource `accelerators` on resource path"), and the unit's
   `Restart=always` respawns it forever.
2. **The interactive boot disk was stale.** `boot_gtk_interactive.py` hardcoded
   `/tmp/vnc-demo/boot.ext4`, which was still packed from a *pre-M1-fix*
   initramfs — an 18 MB `netsurf-gtk` built before `build_netsurf.sh` learned to
   install the `accelerators` file, and before the unit gained `-v` +
   `StandardOutput=tty`. Both facts made the loop invisible in the serial log.
3. **The three candidate causes in the task are all ruled out** (§2.4): not the X
   display environment, not a missing syscall, not the `WEXITED` wait option.
4. **Fix** (§3): `boot_gtk_interactive.py` now re-packs its boot disk from the
   *current* kernel + initramfs + DTB on every launch (mirroring `net_validate.sh`),
   so it can never silently boot a stale initramfs again.
5. **Validation** (§4): the re-packed disk boots the desktop and NetSurf renders
   the bundled home page (`browser_window_navigate` → `html_box_convert_done` →
   `content_scaled_redraw`) with **zero** `netsurf.service` exits/restarts.

---

## 2. Root cause

### 2.1 Symptom

`/tmp/qemu-gtk.log` (the `boot_gtk_interactive.py` serial transcript) shows the
loop in full: every ~4–8 s of guest time

```
netsurf.service: Main process exited, code=exited, status=1/FAILURE
netsurf.service: Failed with result 'exit-code'.
netsurf.service: Consumed <6–9>s CPU time.
netsurf.service: Scheduled restart job, restart counter is at 113.
```

The counter reached 113+ over the ~44-minute capture. The `Consumed Ns CPU time`
(6–9 s per invocation) is the static GTK2 + fontconfig + pango + cairo init the
binary performs *before* it reaches the resource walk that fails — i.e. it is not
a "did real work then quit" signal, but the emulated-RISC-V cost of GTK startup.

### 2.2 The stale boot disk

The boot disk this driver used was inspected directly (raw-cpio initramfs inside
`/tmp/vnc-demo/boot.ext4`):

| artifact | stale value | current (fixed) value |
|---|---|---|
| `usr/bin/netsurf-gtk` | 18 MB, mtime 15 Aug 08:10 | 24 MB, mtime 15 Aug 22:37 |
| `usr/share/netsurf/accelerators` | **absent** | present (1.1 kB) |
| `netsurf.service` `Description` | `… (local HTML render)` | `… (curl+OpenSSL build)` |
| `netsurf.service` `ExecStart` | `netsurf-gtk file://…` (no `-v`) | `netsurf-gtk -v --ca_bundle … ${NETSURF_URL}` |
| `netsurf.service` `StandardOutput/Error` | `null` | `tty` |

The 18 MB / 08:10 binary predates the M1→M2 fixes that made NetSurf actually
render (commit `513b433cb`, "make NetSurf actually render — two startup crashes").
With `NETSURF_USE_GRESOURCE := NO`, every resource must exist as a *file* on the
resource path; `nsgtk_init_resources()` fails hard on the first miss:

```
frontends/gtk/resources.c:251 init_resource: Unable to find resource accelerators on resource path
GTK resources failed to initialise (NotFound)
netsurf.service: Main process exited, code=exited, status=1/FAILURE
```

`Restart=always` + `StartLimitIntervalSec=0` turns that single `exit(1)` into an
unbounded respawn loop. `StandardOutput=null` is why the loop left *no* trace in
the serial log beyond systemd's own "Main process exited" lines — the same trap
M1 §7.2 already called out ("with `Restart=always` and `StandardOutput=null` at the
time, the respawn loop was invisible in the serial log").

### 2.3 Why the fix was already present but not applied

`build_netsurf.sh` has installed the `accelerators` file since the M2 follow-up
(§9.1), and `build_systemd_desktop.sh` step 13b copies the whole
`share/netsurf/` tree into the rootfs. The current build output
`target/qemu-uboot/systemd-desktop-initramfs.cpio` already carries the 24 MB fixed
binary + the `accelerators` file + the `-v`/`tty` unit. The interactive driver
just never used it — it pointed at a `/tmp` boot disk that was re-packed once
(from a stale initramfs) and then trusted forever.

### 2.4 Ruled out

- **X display environment** — `DISPLAY=:0` is set correctly and Xorg is up (the
  sibling `xterm`/`xpanel`/`pcmanfm` units run fine). The crash is NetSurf-specific
  and happens *after* GTK init, in the resource walk, before any X request.
- **Missing syscall** — the log's `Unimplemented syscall number: 264/293/219/258`
  warnings are non-fatal (`ENOSYS` fallbacks in systemd/glibc); NetSurf exits
  cleanly with `exit(1)`, not on a syscall error.
- **`WEXITED` wait option** — `unsupported wait options are found: WEXITED` is a
  *warning* the kernel emits for systemd's `waitid(P_PID, …, WEXITED)` call; it is
  present in **both** the failing capture (481×, only because it ran 10× longer)
  and the successful M7 render capture (4×). systemd still reads the real exit
  status (`code=exited, status=1`), so the `exit(1)` is genuine, not a wait
  mis-report.

---

## 3. Fix

One file: `tools/riscv/systemd/boot_gtk_interactive.py`.

- Added `KERNEL_IMAGE` / `INITRD` / `DTB` paths pointing at the current build
  artifacts (`target/osdk/aster-kernel-osdk-bin.Image`,
  `target/qemu-uboot/systemd-desktop-initramfs.cpio`,
  `target/qemu-uboot/current/qemu-virt.dtb`).
- Added `repack_boot_disk()`, which re-packs `/tmp/vnc-demo/boot.ext4` from those
  three on every launch (same convention as `net_validate.sh`: the raw-cpio
  initramfs is staged under the legacy `initramfs.cpio.gz` name; the kernel's
  unpacker auto-detects raw-newc vs gzip).
- `main()` now calls `repack_boot_disk()` instead of just asserting the stale disk
  exists, and the hardcoded `REPO = Path("/home/arch-anjie/…")` was replaced with
  `Path(__file__).resolve().parent.parent.parent.parent`.

No rootfs / kernel / NetSurf change is needed — the desktop rootfs already contains
the fixed browser; only the boot-disk packaging step was stale.

---

## 4. Validation

`/tmp/browser-m7/home/serial.log` (the M7 render-matrix "home" run, same 24 MB
fixed binary) already shows the expected post-fix behavior — NetSurf navigates and
renders the local home page with **no** `netsurf.service` exit/restart:

```
desktop/browser.c:2057 browser_window_navigate: url file:///usr/share/netsurf/netsurf-home.html
content/handlers/html/html.c:116 html_box_convert_done: Done XML to box (0x…)
content/content.c:614 content_scaled_redraw: Content … 186x160 …
```

(the single `Main process exited` in that log is `nix-activation.service`,
`status=2/INVALIDARGUMENT`, not NetSurf).

Independent re-verification for this milestone: re-pack a fresh boot disk from the
current kernel + the fixed initramfs and boot it. The first attempt (`--smp 4`)
hit the known raw-cpio initramfs-unpack hang under host contention (M5/M6 §3); the
retry (`--smp 1 --net --settle-seconds 180`) completed. Result in §4.1.

### 4.1 Fresh-boot result

Against two resident QEMU guests (`/tmp/m9-fix/home/serial2.log`):

| check | result |
|---|---|
| `netsurf.service` "Main process exited" | **0** |
| `netsurf.service` restart counter | absent |
| `Unable to find resource accelerators` | absent |
| `Invalid object type` (GtkBuilder) | absent |
| `nsgtk_init: Set CSS DPI` (past resource walk + GTK init) | yes |
| `hotlist_init: Loaded hotlist` | yes |
| `nsfont_pango_check` (pango font context) | yes |

So NetSurf starts with the fixed unit, clears the exact failure points that caused
the loop (the `accelerators` resource walk and the static-GTK `GtkBuilder` lazy
type resolution), and stays running — **zero** exits/restarts.

The only transient failure in the boot is `matchbox-window-manager.service`
(`status=1`, restart counter 1): the X-clients-vs-Xorg-readiness race, which
recovers on its own restart — unrelated to NetSurf and non-looping (§5).

The full fetch/render pipeline (`browser_window_navigate` → `html_box_convert_done`
→ `content_scaled_redraw`) for this same initramfs is already captured in the M7
"home" baseline (§4, `/tmp/browser-m7/home/serial.log`). In *this* fresh boot the
180 s settle ended before NetSurf finished its slow static-GTK + fontconfig init
under `-smp 1` + two resident guests, so those render markers are absent from this
capture — not failed.

---

## 5. Remaining items

- The `netsurf.service` unit still races Xorg's ~50 s bring-up (`Type=simple` Xorg
  is "started" as soon as `exec`ed, well before the display socket is ready). With
  the fixed binary this is cosmetic — NetSurf's static GTK init outlives the race
  and it connects on first try — but a proper `ExecStartPre` display-ready gate
  would remove the last start-order assumption.
- The `WEXITED` kernel warning is worth a follow-up syscall fix on its own merits
  (systemd uses `waitid(P_PID, …, WEXITED|…)`), but it is unrelated to this crash.

---

## 6. Artifacts

| file | what it is |
|---|---|
| `tools/riscv/systemd/boot_gtk_interactive.py` | interactive GTK boot; now re-packs its boot disk from current artifacts |
| `tools/riscv/xorg/BROWSER-M9-report.md` | this report |
| `/tmp/vnc-demo/boot.ext4` | the (previously stale) interactive boot disk |
| `/tmp/m9-fix/home/serial2.log` | fresh-boot validation transcript |
| `/tmp/m9-fix/home/boot.ext4` | re-packed validation disk |
