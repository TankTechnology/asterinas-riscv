# BROWSER M5 — default-browser experience + render-quality matrix

**Status:** config/launcher landed & committed; live render matrix blocked by a
kernel-side initramfs-unpack hang under heavy host contention (see §3.2).
**Date:** 2026-08-15
**Scope:** follow M4 (NetSurf renders real HTTPS) with two browser-side goals:
(1) turn NetSurf into the desktop's *default browser* — a home page, a
pre-populated bookmark hotlist, and a panel launcher; (2) measure render quality
across page archetypes (text-only, image+text, complex CSS, charset-less) and
record the gaps. Kernel untouched (the M4 virtio-net fix is the last kernel
change).

---

## 1. Summary

M4 closed the kernel networking gap; M5 moves purely into the browser/desktop
layer.

1. **Default-browser experience** (`d0fa9956d`, §2): a bundled local home page,
   `~/.netsurf/Choices` pinning `homepage_url` to it, a pre-populated
   `~/.netsurf/Hotlist` (bookmarks), and a NetSurf launcher button on the xpanel
   next to the existing xterm launcher.
2. **Render-quality matrix** (`render_matrix.sh`, §3): a harness that boots
   NetSurf against site archetypes on independent `/tmp` disks and captures a
   screenshot + serial log per site. The harness works; the *live* per-site runs
   were blocked by a non-deterministic kernel initramfs-unpack hang that is far
   more likely when the host is oversubscribed.

The render gaps in §4 are therefore split into what is *verified* (M4's
`iana.org` render, the M3.6/M4 image-decode gap, and the static feature ceiling
of the NetSurf 3.9 build) versus what is *blocked pending a quieter host* (the
live per-site screenshots).

---

## 2. Default-browser experience

NetSurf already auto-launched on boot via `netsurf.service`; the gap was that it
opened a bare test page with an empty bookmark menu and no Home target. M5 makes
the launch *feel* like a configured browser.

### 2.1 Home page

`netsurf-home.html` (bundled to `/usr/share/netsurf/`) is a small static
dashboard — a header, a "quick links" card, a stack table — styled with the
same cream/blue palette as the earlier test page so it renders consistently
under NetSurf's libcss. It is now both the **start URL** (the unit's default
`NETSURF_URL`) and the **Home target** (below).

### 2.2 Homepage (Home button)

NetSurf resolves the Home button from the `homepage_url` option, read from
`$HOME/.netsurf/Choices`. M5 ships

```
homepage_url:file:///usr/share/netsurf/netsurf-home.html
```

as `/root/.netsurf/Choices`. The Choices format is `key:value` with the value
taken as everything after the *first* colon, so a `file://` URL parses cleanly
(verified against `utils/nsoption.c:nsoption_read`, which does `strchr(s, ':')`).

### 2.3 Bookmarks (Hotlist)

NetSurf's bookmarks are the "hotlist", loaded from `$HOME/.netsurf/Hotlist` as
an HTML file in the Acorn-Browse format (`<ul>` of `<li><a href=…>`, folders as
`<h4>…</h4><ul>…</ul>`). M5 ships a two-folder hotlist (Asterinas / Reference)
with IANA, NetSurf, info.cern.ch, Hacker News, and Wikipedia. The format was
verified against `desktop/hotlist.c:hotlist_load*` so the entries parse.

### 2.4 Launcher

`xpanel.c` gained a second launcher button (a "web page" glyph) to the right of
the xterm button; clicking it forks `netsurf-gtk`. `build_xpanel.sh` cross
compiles the pure-X11 panel as a static riscv64 binary (libX11 closure
`-lX11 -lpthread -lxcb -lXau`) into `target/riscv-cross/usr/bin/xpanel`.

### 2.5 Wiring

`build_systemd_desktop.sh` step 13e copies the home page into
`/usr/share/netsurf/` and the Choices/Hotlist into `/root/.netsurf/`. An
optional `NETSURF_URL` env var bakes a per-site start URL into
`/etc/netsurf.conf` (read by the unit's `EnvironmentFile`), which is what the
render matrix uses to point one initramfs at each site.

---

## 3. Render-quality matrix

`tools/riscv/xorg/render_matrix.sh` builds a per-site initramfs + boot disk and
boots `boot_systemd_desktop.py --net` on an independent `/tmp` disk, then
converts the QEMU screendump to PNG. Two boot-driver flags were added to make
this parallel-safe: `--mon-sock` (unique monitor socket per guest) and `--smp`.

Site archetypes:

| name | URL | archetype |
|---|---|---|
| home | `file:///usr/share/netsurf/netsurf-home.html` | local dashboard (also the config smoke test) |
| iana | `https://www.iana.org/` | baseline (M4 verified: HTML+CSS+SVG) |
| hackernews | `https://news.ycombinator.com/` | text-only table list |
| wikipedia | `https://en.wikipedia.org/wiki/RISC-V` | image+text (infobox/thumbnails) |
| example | `https://example.com/` | charset-less (BadEncoding) |
| csszengarden | `https://www.csszengarden.com/` | complex CSS (float/positioning showcase) |

### 3.1 Harness verification

The prepare stage is verified end-to-end: each site gets a ~163 MiB independent
`/tmp/browser-m5/<site>/boot.ext4` (kernel `asterinas.booti` + the raw-newc
`initramfs.cpio.gz` + `qemu-virt.dtb`) and the boot driver drives the U-Boot
`booti` handoff with the simple-framebuffer DTB injection. The initramfs lists
cleanly (`cpio -it`, 864 entries).

### 3.2 Blocking issue: non-deterministic initramfs-unpack hang

The live boots do not reliably reach userspace. Serial logs stall at

```
[kernel] unpacking initramfs.cpio to rootfs ...
```

and never emit the `>>> systemd init: … <<<` launcher marker. The `initramfs`
unpack loop in `kernel/src/fs/rootfs.rs` (the `CpioDecoder` → per-entry append
into the RAM filesystem) does not complete within 600 s of settle on a loaded
host. Two earlier boots (at lower load) did get past unpacking and reached
NetSurf, so this is the same class of non-deterministic large-initramfs hang the
rootfs build already documents for gzip (the `build_systemd_desktop.sh` header
notes the zune-inflate decoder hangs on >16 MB gzip inputs and mandates raw
cpio) — now also reproducible against the raw-cpio path when the guest is
starved.

Contributing factor: at run time the host carried three other autonomous tasks
(DRM-M3, POLISH-M6, a fourth) whose activity held load ~28–36 on 16 threads, so
the guest clock ran well under real-time and the hang's trigger window widened.
This is environmental + kernel-side, both outside the M5 "kernel untouched"
boundary; no kernel change was made.

---

## 4. Rendering gaps

### 4.1 Verified (from M4 / M3.6, unchanged)

| gap | detail |
|---|---|
| Charset-less pages | `example.com` serves `text/html` with no `charset=`, tripping NetSurf's `BadEncoding` charset fallback (M4 §5) |
| In-page `<img>` deferred decode | `image_cache_add … bitmap (nil)` — large images are speculatively deferred and never decoded (M3.6, M4 §5) |
| No JavaScript | `NETSURF_USE_DUKTAPE := NO` at build time |

### 4.2 Static feature ceiling (from the M5 build config)

The M5 NetSurf build is a mostly-static GTK2 3.9 with:

- **Images:** BMP/ICO, GIF, JPEG, PNG, SVG (libsvgtiny) — **no WebP**
  (`NETSURF_USE_WEBP := NO`), no librsvg (libsvgtiny's limited SVG subset only).
- **Layout/CSS:** libcss (CSS 2.1 + a CSS3-selector subset). No flexbox, no
  CSS grid, no transforms/transitions/animation, no `position:sticky` — the
  systematic gaps modern image+text and "complex CSS" sites expose.
- **DOM:** libdom/libhubbub (HTML5 tree builder), but no script execution.

### 4.3 Per-site results

The live per-site screenshots are not yet available (blocked by §3.2). The one
*verified* render remains M4's `https://www.iana.org/` — a real HTTPS page with
HTML + CSS + favicon + SVG logo, redrawn at 1024×881 (see
`/tmp/m4-iana.png`). Re-running the matrix against a quieter host is the
remaining step; the harness is ready and the `--settle-seconds`/`--mon-sock`
knobs are in place to retry without touching the shared boot disk.

---

## 5. Remaining items

- Re-run `render_matrix.sh` when the host is idle to collect the per-site
  screenshots and fill §4.3 (add a per-boot retry that re-boots when
  `rootfs is ready` is absent, to ride out the §3.2 hang).
- The in-page `<img>` deferred-decode path is still the largest single
  image-rendering gap (unchanged from M3.6/M4).
- JavaScript remains off; enabling duktape needs a riscv64 duktape cross-build.
- No WebP; adding it needs a riscv64 libwebp static build.

---

## 6. Artifacts

| file | what it is |
|---|---|
| `tools/riscv/xorg/BROWSER-M5-report.md` | this report |
| `tools/riscv/xorg/netsurf-home.html` | bundled home/start page |
| `tools/riscv/xorg/netsurf-hotlist.html` | pre-populated bookmark hotlist |
| `tools/riscv/xorg/netsurf-choices` | `homepage_url` Choices file |
| `tools/riscv/xorg/build_xpanel.sh` | static riscv64 xpanel build |
| `tools/riscv/xorg/xpanel.c` | +NetSurf launcher button |
| `tools/riscv/xorg/render_matrix.sh` | per-site boot + screenshot harness |
| `tools/riscv/systemd/build_systemd_desktop.sh` | step 13e + `NETSURF_URL` env |
| `tools/riscv/systemd/units/netsurf.service` | default URL → home page |
| `tools/riscv/systemd/boot_systemd_desktop.py` | `--mon-sock` / `--smp` |
| `/tmp/browser-m5/<site>/serial.log` | per-site serial (shows the §3.2 stall) |
