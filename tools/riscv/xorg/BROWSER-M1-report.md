# BROWSER M1 — NetSurf 3.9 (GTK2) cross-compiled for the Asterinas RISC-V desktop

**Status:** milestone reached — `netsurf-gtk` renders a local HTML page on the
systemd desktop, **with CSS colour styling confirmed** (M2 follow-up, see §9)
**Date:** 2026-08-15
**Scope:** evaluate the lightweight browser NetSurf's build dependencies against
the existing `riscv64` cross prefix, cross-compile the feasible subset, and run the
result inside QEMU on a local HTML file.

---

## 1. Milestone summary

NetSurf is the last missing piece of the desktop (WM + panel + file manager +
terminal already run under systemd). This milestone cross-compiled **NetSurf 3.9
with the GTK2 frontend** (`nsgtk`, 18.2 MB) as a mostly-static binary — it links
GTK2/Pango/Cairo/libcss/libdom/libcurl/etc. statically and only glibc dynamically,
exactly like the existing `pcmanfm`/`matchbox-wm`. It is bundled into the systemd
desktop rootfs and launched by a new `netsurf.service` unit that opens
`file:///usr/share/netsurf/netsurf-test.html`.

The whole NetSurf core is built from the upstream `netsurf-all-3.9` bundle (13
libraries + the frontend), and one external dependency (`libcurl`) was added to the
cross prefix.

---

## 2. Dependency gap: NetSurf 3.9 GTK2 vs `target/riscv-cross/usr`

NetSurf's GTK frontend requires at minimum GTK 2.12 (3.9 defaults to GTK2, so no
`NETSURF_GTK_MAJOR` override was needed). Mapping the Makefile's
`pkg_config_find_and_add*` / `feature_switch` list against what was already in the
cross prefix:

| Dependency | Role | Present? |
|---|---|---|
| `gtk+-2.0` + `gdk-x11-2.0` + `gdk-pixbuf` + `atk` + `pango` + `cairo` | GTK frontend | ✅ static `.a` |
| `glib-2.0` / `gobject` / `gmodule` / `gthread` / `gio` | core | ✅ |
| `libpng` (libpng16.pc) | PNG images | ✅ |
| `libjpeg` | JPEG images | ✅ |
| `zlib` (`-lz`) | compression | ✅ |
| `libexpat` | XML (libdom dep) | ✅ |
| `freetype` / `fontconfig` / `fribidi` / `harfbuzz` | text shaping | ✅ |
| iconv | charset conversion | ✅ glibc internal (`LIBICONV_PLUG`) |
| `libcurl` | HTTP/HTTPS fetch | ❌ → **built (no TLS)** |
| `openssl` | HTTPS / TLS | ❌ → **deferred** (future milestone) |
| `librsvg-2.0` | SVG via Cairo | ❌ → disabled (libsvgtiny used instead) |
| `libwebp` | WEBP images | ❌ → disabled |
| `libcss` `libdom` `libhubbub` `libparserutils` `libwapcaplet` `libnsutils` `libutf8proc` `libnsbmp` `libnsgif` `libnspsl` `libnslog` `libsvgtiny` `librosprite` | NetSurf core | ❌ → **built from source** |

The "highly recommended" network stack (curl + openssl) is the only real gap. Image
decoding (PNG/JPEG/GIF/BMP) and text are fully covered by the existing prefix, so a
local `file://` render needs no further external work.

---

## 3. Building the NetSurf core libraries

NetSurf uses its own GNU-make buildsystem (bundled in `netsurf-all-3.9`), not
autotools. The cross convention differs from the GTK stack: the *libraries* take
`HOST=<target ABI>` (riscv64-linux-gnu) and `NSSHARED=<buildsystem>`; the
*frontend* takes `HOST=uname -s` (build platform) and cross via `CC`/`PKG_CONFIG`.

`tools/riscv/xorg/build_netsurf.sh` builds the 13 core libraries in dependency
order with `make install COMPONENT_TYPE=lib-static`, producing static `.a` + `.pc`
files in `target/riscv-cross/usr`:

```
libnslog libwapcaplet libparserutils libcss libhubbub libdom
libnsbmp libnsgif librosprite libnsutils libutf8proc libnspsl libsvgtiny
```

Two buildsystem gotchas had to be worked around:

1. **Tool-prefix mangling.** `Makefile.tools` reverse-engineers the binutils
   prefix from the compiler path and mangles `riscv64-linux-gnu` into
   `/usr/bin/riscv64/linux/gnu/-ar`. Fix: export `AR=riscv64-linux-gnu-ar` (and
   `CXX`) explicitly so `$(origin AR) != default` skips the guess.
2. **`-Werror`.** Every library hardcodes `-Werror`; GCC 15 flags legacy-C
   warnings. Fix: pass `WARNFLAGS=-Wno-error` on the command line (overrides the
   Makefile's `:=`), plus `-Wno-implicit-function-declaration -Wno-implicit-int
   -fcommon` in `CFLAGS`.

The `pkg-config-static` wrapper (`--static`) is exported as both `PKG_CONFIG` and
`PKGCONFIG` so the buildsystem's `pkg_config_find_and_add` pulls `Requires.private`
when linking.

---

## 4. libcurl (header + static lib, no TLS)

NetSurf's `content/fetch.c` includes `content/fetchers/curl.h` **unconditionally**,
and that header includes `<curl/curl.h>` — so the curl header is required to compile
even when the curl fetcher is compiled out. `curl.c` also includes `<openssl/ssl.h>`
unconditionally, so the *fetcher* cannot be enabled without OpenSSL.

`tools/riscv/xorg/build_libcurl.sh` therefore builds **libcurl 8.14.1 static, no
TLS** (`--without-ssl` and every other heavy dep disabled). This supplies
`curl/curl.h` + `libcurl.a` + `libcurl.pc`. For M1 the curl fetcher stays
**disabled** (`NETSURF_USE_CURL := NO`) — the browser renders `file://`/`about:`/
`data:`/`resource:` but has no HTTP/HTTPS. Enabling it is a follow-up that adds
OpenSSL (see §8).

---

## 5. Building the `nsgtk` GTK2 frontend

The frontend is configured via a generated `Makefile.config` (downstream, not
upstream):

```make
override NETSURF_USE_CURL := NO        # curl fetcher needs OpenSSL (deferred)
override NETSURF_USE_OPENSSL := NO
override NETSURF_USE_RSVG := NO        # libsvgtiny instead
override NETSURF_USE_NSSVG := YES
override NETSURF_USE_ROSPRITE := YES
override NETSURF_USE_WEBP := NO
override NETSURF_USE_DUKTAPE := NO     # no JS (would need nsgenbind)
override NETSURF_USE_GRESOURCE := NO   # would need glib-compile-resources host tool
override NETSURF_USE_INLINE_PIXBUF := YES
override NETSURF_USE_NSPSL := YES
override NETSURF_USE_NSLOG := YES
```

Built with `make TARGET=gtk CC=riscv64-linux-gnu-gcc PKG_CONFIG=<pkg-config-static>`.
One more frontend quirk: the `INLINE_PIXBUF` rule writes `favicon.c` into
`$(OBJROOT)` without depending on the `created` dir, so under `-j` it races the
directory creation — fixed by pre-creating `build/Linux-gtk` before `make`.

Result: `nsgtk` (18.2 MB), `NEEDED` only `libm.so.6`/`libc.so.6`/`ld-linux` —
everything else is statically linked, matching the `pcmanfm`/`matchbox-wm` pattern.
The binary plus resources (default.css/quirks.css/internal.css, icons, throbber,
translations, `welcome.html`) install to `target/riscv-cross/usr/share/netsurf/`
(1.3 MB total).

---

## 6. Integration into the systemd desktop

- **`netsurf.service`** (new unit) launches
  `/usr/bin/netsurf-gtk file:///usr/share/netsurf/netsurf-test.html` with
  `DISPLAY=:0`, `FONTCONFIG_FILE`, and `NETSURFRES=/usr/share/netsurf`; it is
  `After=xorg.service matchbox-window-manager.service`, `Restart=on-failure`.
- **`graphical.target`** now `Wants=` the `netsurf.service`.
- **`build_systemd_desktop.sh`** copies `netsurf-gtk` into the desktop client list,
  and copies `share/netsurf/` + `netsurf-test.html` into the rootfs. The `nsgtk`
  baked `GTK_RESPATH` (the *host* cross prefix) is bridged to the guest's
  `/usr/share/netsurf` by the existing baked-host-path symlink (`…/target/
  riscv-cross/usr → /usr`), and `NETSURFRES` points at the same place as a
  belt-and-suspenders.

---

## 7. QEMU render test

### 7.1 A boot-loading ceiling had to be raised

The first boot attempt failed before the kernel started: `booti` reported
`Could not find a valid device tree`. The systemd boot driver loads the DTB at
`DTB_LOAD = 0x8800_0000` and the initramfs at `INITRD_LOAD = 0x8300_0000`. The
previous initramfs (~77 MB) ended at `0x87d0_0000`, below the DTB slot; adding
NetSurf (+~19 MB) pushed it to ~93 MB, whose upper bound (`0x8300_0000 +
0x5c11e00 = 0x8e11e00`) crossed `0x8800_0000`, so the `ext4load` of the
initramfs clobbered the FDT. The fix relocates `DTB_LOAD` to `0x9000_0000`
(RAM is 2 GiB, `0x8000_0000 .. 0x1_0000_0000`), well above the load ceiling —
`tools/riscv/systemd/boot_systemd_desktop.py`.

### 7.2 Render

The second boot reached `graphical.target`, Xorg brought up its input devices
(`Adding extended input device "keyboard"`), and every desktop unit started
cleanly, including:

```
[  OK  ] Started NetSurf web browser (local HTML render).
```

`netsurf-gtk` stayed running for the whole 90 s settle (no crash / restart in the
log — `Restart=always` would have logged a respawn if it had exited). The
screenshot (`target/demo/asterinas-desktop.png`, 1280×1024) pixel histogram shows
the desktop with its four windows: a large white region (88%), the `#202028`
xpanel bar, `#DCDAD5` GTK chrome, `#496179` matchbox titlebars, and ~12.5 k black
pixels of rendered text.

**Status: NetSurf was actually crash-looping here, not rendering.** This section's
original conclusion ("renders the local HTML's text content") was wrong — the
~12.5 k black pixels were xterm/pcmanfm text, not NetSurf. With `Restart=always`
and `StandardOutput=null` at the time, the respawn loop was invisible in the
serial log. The M2 follow-up (§9) routed NetSurf's log to serial and found two
startup crashes; both are fixed and CSS colour styling now renders.

---

## 8. Known limitations / next steps

- **No network.** The curl fetcher + OpenSSL are disabled, so `http://`/`https://`
  fetch is unavailable (only `file://`, `about:`, `data:`, `resource:`). Enabling
  it needs an OpenSSL static cross-build (and `NETSURF_USE_CURL := YES`), plus a CA
  bundle in the rootfs.
- **CSS colour styling — RESOLVED (M2).** The absent colours were not a libcss
  gap: NetSurf was crash-looping and never drew a window. Once the two startup
  crashes (§9) were fixed, the screenshot shows `body` cream (`#f4e8d0`), the
  `h1`/`.banner` blue (`#1a4f8b`), and the `th` grey (`#dcdad5`) all render.
- **No JavaScript.** `NETSURF_USE_DUKTAPE := NO` avoids the `nsgenbind` host tool.
- **No SVG via librsvg** (libsvgtiny covers a small SVG subset instead) and no
  WEBP.
- The `gtk2.ui` dialogs (cookies, downloads, …) are bundled but untested — the
  milestone only exercises the single-window local render.

---

## 9. M2 follow-up: two startup crashes (and the fix)

M1 shipped believing NetSurf "rendered the text" (§7.2). Routing NetSurf's log to
serial (075bc5552) plus a `-v` flag in `netsurf.service` revealed the truth:
`nsgtk` was exiting with status 1 on startup and systemd's `Restart=always` was
respawn-looping it (`restart counter is at 11`). It never opened a window — the
screenshot's text was xterm/pcmanfm. Two independent causes:

### 9.1 Missing `accelerators` resource

With `NETSURF_USE_GRESOURCE := NO`, every resource must exist as a *file* on the
resource path. `nsgtk_init_resources()` iterates the `direct_resource[]` table and
fails hard on the first miss:

```
(…) frontends/gtk/resources.c:251 init_resource: Unable to find resource accelerators on resource path
GTK resources failed to initialise (NotFound)
netsurf.service: Main process exited, code=exited, status=1/FAILURE
```

`accelerators` is in the source (`frontends/gtk/res/accelerators`) and in the
gresource XML, but the upstream `install-gtk` target's `GTK_RESOURCES_LIST` omits
it — upstream never notices because gresource builds embed it. Fix:
`build_netsurf.sh` now installs the file explicitly.

### 9.2 Static GTK + GtkBuilder lazy type resolution

After §9.1, NetSurf got further but died on the first `.ui` file:

```
(…) nsgtk_builder_new_from_resname: Unable to add UI builder … tabcontents.gtk2.ui "Invalid object type `GtkStatusbar'"
(…) gui_window_create: Tab contents UI builder init failed
NetSurf gtk initialise failed (BadParameter)
```

`GtkBuilder` resolves widget types lazily: `g_type_from_name("GtkStatusbar")`
misses (never registered), then `_gtk_builder_resolve_type_lazily()` does
`g_module_symbol("gtk_statusbar_get_type")` (dlsym). NetSurf never calls
`GtkStatusbar` directly (it's only in the `.ui`), so the static linker dropped
`gtkstatusbar.o` from the binary — verified: the `"GtkStatusbar"` type-name string
was absent from `nsgtk` while `"GtkHPaned"`/`"GtkLayout"` (which NetSurf *does*
touch) were present. Fix: link the frontend with

```
-Wl,--export-dynamic -Wl,--whole-archive -lgtk-x11-2.0 -Wl,--no-whole-archive
```

`--whole-archive` force-includes every libgtk object (adding only ~0.8 MB — the
previously-dropped widget objects), and `--export-dynamic` exposes their
`*_get_type` symbols to `dlsym`. All `.ui`-only types (`GtkStatusbar`,
`GtkHPaned`, `GtkLayout`, …) now resolve. (Note: the `LDFLAGS` change does not
invalidate make's dependency graph, so the stale binary must be deleted before
relinking.)

### 9.3 Result

After both fixes the boot log shows the full fetch/render pipeline completing
without a restart:

```
content__init: url file:///usr/share/netsurf/netsurf-test.html
html_css_new_stylesheets: 3 fetches active
content__init: url x-ns-css:0            ← the inline <style> block
html_convert_css_callback: done stylesheet slot 4 'x-ns-css:0'
html_box_convert_done: Done XML to box
content_scaled_redraw: Content … 272x234
```

and the screenshot histogram now contains the author CSS colours (vs. M1's all
white/black): cream `#f4e8d0` (180 k px), blue `#1a4f8b` (34 k px), grey
`#dcdad5` (84 k px). libcss 0.9.0 parses and applies the `<style>` block
correctly; the M1 "CSS colour" mystery was entirely the crash-loop.

## 10. Artifacts

| file | what it is |
|---|---|
| `tools/riscv/xorg/BROWSER-M1-report.md` | this report |
| `tools/riscv/xorg/build_netsurf.sh` | builds 13 core libs + `nsgtk` GTK2 frontend |
| `tools/riscv/xorg/build_libcurl.sh` | static libcurl (with OpenSSL) cross-build |
| `tools/riscv/xorg/netsurf-test.html` | the local render test page |
| `tools/riscv/systemd/units/netsurf.service` | launches `netsurf-gtk` (URL via `/etc/netsurf.conf`) |
| `tools/riscv/systemd/units/graphical.target` | now `Wants=` netsurf.service |
| `tools/riscv/systemd/build_systemd_desktop.sh` | bundles netsurf-gtk + resources |
| `target/riscv-cross/usr/bin/netsurf-gtk` | the built browser (23.5 MB after curl+OpenSSL) |
| `target/riscv-cross/usr/lib/libcss.a` `libdom.a` … | the 13 static core libs |
| `target/demo/asterinas-desktop.png` | the milestone screenshot |

---

## 11. M3 follow-up: HTTPS fetch — browser side verified, blocked by kernel networking

**Status:** the curl+OpenSSL browser stack is verified *active at runtime* and the
HTTPS fetch *reaches the curl fetcher*; the actual TLS handshake is blocked by a
**kernel network-stack gap on RISC-V** (not a NetSurf/curl/OpenSSL issue).
**Date:** 2026-08-15

### 11.1 Runtime proof that the curl+OpenSSL build is active

The rebuild (§5 now has `NETSURF_USE_CURL/OPENSSL := YES`) is not just linked —
it registers and initialises at startup, logged to serial via the `-v` flag
(commit 075bc5552's tty routing):

```
content/fetchers/curl.c:1493 fetch_curl_register: curl_version libcurl/8.14.1 OpenSSL/3.0.15 zlib/1.3.1
content/fetchers/curl.c:1583 fetch_curl_register: cURL linked against openssl
content/fetchers/curl.c:176  fetch_curl_initialise: Initialise cURL fetcher for http
content/fetchers/curl.c:176  fetch_curl_initialise: Initialise cURL fetcher for https
utils/nsoption.c:806 nsoption_commandline: ca_bundle = /etc/ssl/certs/ca-certificates.crt
```

Four facts confirmed: (1) `libcurl 8.14.1` compiled **with OpenSSL 3.0.15**; (2)
explicit `cURL linked against openssl`; (3) **both** the `http` and `https`
fetchers initialise; (4) the CA bundle path resolves. The binary is 23.5 MB
(was 18.2 MB) — the ~5 MB delta is static libcurl + libssl + libcrypto. `nm`
shows no curl/SSL symbols because `NETSURF_STRIP_BINARY := YES`; `strings`
confirms them (`fetch_curl_multi`, `SSL_CTX_set_max_early_data`, …).

### 11.2 Boot-verify infrastructure (independent /tmp disk)

The shared `target/qemu-uboot/current/boot.ext4` is in use by the VNC QEMU, so
all of this boots from an independent copy. `boot_systemd_desktop.py` gained
`--boot-disk` (override path), `--net` (attach `virtio-net-device` via QEMU
slirp user networking), and `--settle-seconds` (drain serial after desktop-up —
NetSurf starts *after* the Xorg-input milestone that ends normal collection, so
without this its fetch log is never captured). The rootfs gained glibc
name-resolution (libnss_files/libnss_dns/libresolv + nsswitch.conf +
resolv.conf → `nameserver 10.0.2.3`) and `netsurf.service` takes its start URL
from `/etc/netsurf.conf` (`NETSURF_URL`).

### 11.3 The fetch reaches curl, then dies in the kernel

With `--net` and `NETSURF_URL=https://example.com/`, the browser navigates and
the curl fetcher attempts the request:

```
content/fetchers/curl.c:842 fetch_curl_stop: fetch 0x…, url 'https://example.com/'
content/fetchers/curl.c:1128 fetch_curl_done: Unknown cURL response code 6
frontends/gtk/gui.c:513 nsgtk_warning: Could not resolve hostname
```

`code 6` = `CURLE_COULDNT_RESOLVE_HOST`. The DNS query (a UDP send to the
slirp resolver at 10.0.2.3) never returns. To isolate DNS from TCP, a second
run pinned `example.com` in `/etc/hosts` (so libnss_files resolves it without
any UDP), which changed the failure to:

```
content/fetchers/curl.c:1128 fetch_curl_done: Unknown cURL response code 28
frontends/gtk/gui.c:513 nsgtk_warning: Timeout was reached
```

`code 28` = `CURLE_OPERATION_TIMEDOUT` — the TCP connect to `104.20.23.154:443`
never completed. So **neither UDP (DNS) nor TCP moves packets**: the kernel's
network stack is not forwarding on RISC-V.

### 11.4 Root cause is kernel-side, not browser-side

The evidence that the *hardware + slirp* path is fine while the *kernel driver*
is not: U-Boot — using its own virtio-net driver over the **same** device —
already ran DHCP successfully before handing off:

```
Net:   eth0: virtio-net#3
DHCP client bound to address 10.0.2.15 (2 ms)
```

and the kernel discovers the device (negotiating features, dropping the ones it
doesn't support) but warns:

```
virtio: Network: `single_interrupt` ignored: no support for virtio-mmio devices
```

The kernel's `aster-virtio` network device has a full TX/RX + smoltcp
(`aster_bigtcp`) poll implementation and registers `Virtio-Net`/`eth0`
(10.0.2.15/24, gw 10.0.2.2 — exactly the slirp defaults), but no packet reaches
the wire. This is a **pre-existing RISC-V kernel-networking gap** (interrupt
routing / queue-notify on the virtio-MMIO transport), orthogonal to the browser.

### 11.5 Conclusion and next step

- **Browser side: done.** curl+OpenSSL is compiled in, registered, and attempts
  HTTPS with the CA bundle in place. Enabling the curl fetcher did not
  destabilise startup — no crash-loop (the `Restart=always` respawn that hid the
  M2 bugs is absent).
- **Blocker: kernel networking.** Actual `https://` fetch requires the
  virtio-net MMIO path to move packets; that is a kernel milestone, not a
  browser one.
- **Next:** full webpage *rendering* verification can proceed on a local
  `file://` page (unaffected by the network gap) — a richer HTML/CSS page than
  the M1 test, exercised with a longer settle and a pixel-histogram check.
