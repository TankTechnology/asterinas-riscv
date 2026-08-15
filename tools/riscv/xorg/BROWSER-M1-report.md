# BROWSER M1 — NetSurf 3.9 (GTK2) cross-compiled for the Asterinas RISC-V desktop

**Status:** milestone reached — `netsurf-gtk` renders a local HTML page on the
systemd desktop
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

**Status: NetSurf runs and renders the local HTML's text content.** The one thing
not confirmed visually is the CSS *colour* styling — the test page's `body
{ background: #f4e8d0 }` and `h1 { color: #1a4f8b }` do not appear in the
histogram (the page background reads white). This is either a libcss 0.9.0 gap on
those declarations or the UA `default.css` overriding them; `netsurf.service` now
routes `StandardOutput/Error=tty` so NetSurf's own fetch/render log is captured on
the next boot to pin it down (see §8).

---

## 8. Known limitations / next steps

- **No network.** The curl fetcher + OpenSSL are disabled, so `http://`/`https://`
  fetch is unavailable (only `file://`, `about:`, `data:`, `resource:`). Enabling
  it needs an OpenSSL static cross-build (and `NETSURF_USE_CURL := YES`), plus a CA
  bundle in the rootfs.
- **CSS colour styling unconfirmed.** Text renders but the `<style>` block's
  colours do not appear in the screenshot; investigate whether libcss 0.9.0 drops
  those declarations or `default.css` overrides them (next boot now logs NetSurf's
  render output to the serial console for diagnosis).
- **No JavaScript.** `NETSURF_USE_DUKTAPE := NO` avoids the `nsgenbind` host tool.
- **No SVG via librsvg** (libsvgtiny covers a small SVG subset instead) and no
  WEBP.
- The `gtk2.ui` dialogs (cookies, downloads, …) are bundled but untested — the
  milestone only exercises the single-window local render.

---

## 9. Artifacts

| file | what it is |
|---|---|
| `tools/riscv/xorg/BROWSER-M1-report.md` | this report |
| `tools/riscv/xorg/build_netsurf.sh` | builds 13 core libs + `nsgtk` GTK2 frontend |
| `tools/riscv/xorg/build_libcurl.sh` | static libcurl (no TLS) cross-build |
| `tools/riscv/xorg/netsurf-test.html` | the local render test page |
| `tools/riscv/systemd/units/netsurf.service` | launches `netsurf-gtk` on the test page |
| `tools/riscv/systemd/units/graphical.target` | now `Wants=` netsurf.service |
| `tools/riscv/systemd/build_systemd_desktop.sh` | bundles netsurf-gtk + resources |
| `target/riscv-cross/usr/bin/netsurf-gtk` | the built browser (18.2 MB) |
| `target/riscv-cross/usr/lib/libcss.a` `libdom.a` … | the 13 static core libs |
| `target/demo/asterinas-desktop.png` | the milestone screenshot |
