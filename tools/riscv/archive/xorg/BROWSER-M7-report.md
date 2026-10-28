# BROWSER M7 — charset fallback (BadEncoding) fix + GIF refutation

**Status:** charset fix landed & matrix-validated; GIF "bug" refuted (window
clipping). Kernel untouched.
**Date:** 2026-08-15
**Scope:** follow M6. Two goals from the M6 gap list: (1) fix the highest-value
rendering gap — the `BadEncoding` rejection of every charset-less page — and
(2) close out the remaining image gap (GIF). CSS layout is re-surveyed.

---

## 1. Summary

1. **Charset fallback fixed (§2).** `info.cern.ch`, `example.com` and
   `rfc-editor.org` serve `text/html` with **no `charset=`** in the HTTP
   header and no `<meta charset>`. NetSurf auto-detects the encoding as
   Windows-1252 and converts the page to UTF-8 through glibc `iconv`. The
   riscv64 guest's glibc dynamic runtime has **no gconv modules**, so
   `iconv_open("UTF-8","Windows-1252")` fails and the whole page dies with
   `NSERROR_BAD_ENCODING` ("BadEncoding"). Fix: build `libparserutils` with
   `-DWITHOUT_ICONV_FILTER` so the parser's input filter uses NetSurf's own
   codec tables instead of glibc iconv. **Two of the three sites now render;
   the third's charset error is gone but its fetch now hits the kernel-side
   flaky-net receive error.**
2. **GIF "bug" refuted (§3).** M6 §5/§7.1 carried a gap "single-frame GIF
   leaves `frame_image` unset". Tracing `nsgif_redraw` → `nsgif_get_frame` →
   `gif_decode_frame` shows the decode path is correct. The M6 observation was
   **window clipping**: the imagetest page's GIF row is the 4th row, below the
   JPEG row, and both fall below the NetSurf window fold (~1004 px) — the M6
   screenshot contains no JPEG plasma pixels either. A new `giftest` page
   places the GIF at the top, above the fold.
3. **CSS layout (§4).** No new tractable CSS bug: the libcss 2.1 feature
   ceiling (no flexbox/grid/transform/sticky) is unchanged, and the pages that
   do render (home's floats/table/text, man7) lay out correctly.

---

## 2. Charset fallback — BadEncoding

### 2.1 Root cause

The three failing sites are pure-ASCII pages served as `text/html` with no
`charset`:

```
$ curl -sSI https://example.com/          | grep -i content-type
content-type: text/html                    # no charset=
$ curl -sSI https://info.cern.ch/         | grep -i Content-Type
Content-Type: text/html                    # no charset=
$ curl -sSI https://www.rfc-editor.org/rfc/rfc768.html | grep -i content-type
content-type: text/html                    # no charset=
```

NetSurf's HTML path is: `html_create_html_data` (no header charset → parser
created with `enc = NULL`) → `html_process_data` →
`dom_hubbub_parser_parse_chunk`. The hubbub parser's input stream
(`libparserutils/src/input/filter.c`) auto-detects the encoding
(`hubbub_charset_extract` returns Windows-1252 for ASCII), then converts the
incoming bytes to UTF-8 via glibc `iconv`:

```c
input->cd = iconv_open(parserutils_charset_mibenum_to_name(int_enc), /* "UTF-8" */
                       parserutils_charset_mibenum_to_name(mibenum));/* "Windows-1252" */
if (input->cd == (iconv_t)-1)
        return (errno == EINVAL) ? PARSERUTILS_BADENCODING : PARSERUTILS_NOMEM;
```

`PARSERUTILS_BADENCODING → HUBBUB_BADENCODING → NSERROR_BAD_ENCODING`, which the
GTK frontend surfaces as the "BadEncoding" error dialog and aborts the page.

glibc's `iconv_open` handles UTF-8↔UTF-8, UTF-8↔ISO-8859-1 and a handful of
others **internally**, but Windows-1252 and most other encodings are provided by
`gconv` modules — separate `.so` files that glibc loads at runtime from
`/usr/lib/gconv/`. The guest initramfs ships the glibc dynamic runtime
(`ld-linux`, `libc.so.6`, `libm.so.6`, …) but **no gconv directory**, so
`iconv_open("UTF-8","Windows-1252")` returns `(iconv_t)-1` with `EINVAL`. Pages
that *do* declare `charset=UTF-8` (iana.org, man7, the bundled local pages) work
because UTF-8 needs no gconv module — which is why the gap was invisible on the
M5/M6 render sites and only showed up on the charset-less archetypes.

### 2.2 Fix

`libparserutils` has a first-class "no iconv" configuration for exactly this
situation. `Makefile.config` documents it:

```
# Disable use of iconv in the input filter
# CFLAGS := $(CFLAGS) -DWITHOUT_ICONV_FILTER
```

With `WITHOUT_ICONV_FILTER` defined, `filter_set_encoding` swaps the glibc iconv
call for NetSurf's bundled codec tables:

```c
#else  /* WITHOUT_ICONV_FILTER */
        error = parserutils_charset_codec_create(enc, &input->read_codec);
```

`parserutils_charset_codec_create` dispatches to `codec_8859.c` (ISO-8859-1..16),
`codec_ext8.c` (Windows-1250..1258), `codec_utf8.c`, `codec_utf16.c` and
`codec_ascii.c` — all pure C tables, no runtime module loading. The flag is read
only by `filter.c`, so defining it globally in the cross-build's `CFLAGS` is
harmless to the other components. The change is one line in `build_netsurf.sh`
(plus a comment): add `-DWITHOUT_ICONV_FILTER` to the exported `CFLAGS`.

`make` does not track the env-`CFLAGS` change as a dependency, so the first
rebuild re-installed a stale `libparserutils.a` (still `U iconv`). Deleting
`libparserutils/build-*-release-lib-static` and the frontend `build/Linux-gtk`
and re-running `build_netsurf.sh all` produced a clean rebuild — the new
`libparserutils.a` has no `U iconv`/`iconv_open`/`iconv_close` and the `nsgtk`
binary grew by the size of the internal codec tables.

### 2.3 Validation

Per-site matrix run (same `render_matrix.sh` harness, `OUT_DIR=/tmp/browser-m7`,
`-smp 1`), with the rebuilt frontend:

| site | M6 outcome | M7 outcome |
|---|---|---|
| infocern | ✗ `BadEncoding` | ✓ renders — `box=1 redraw=2`, black text + links, `badenc=0` |
| example | ✗ `BadEncoding` | ✓ renders — `box=1 redraw=2`, content + text, `badenc=0` |
| rfc | ✗ `BadEncoding` | ✓ charset fixed (`badenc=0`), but fetch died `code 56` (kernel-side) |

`box` = `html_box_convert_done` seen (HTML parsed → box tree), `redraw` =
`content_scaled_redraw` seen, `badenc` = `BadEncoding` occurrences in the serial
log. In M6 all three showed `BadEncoding` before any box conversion; in M7
`html_box_convert_done` now fires for infocern and example and `badenc=0` for
all three.

The `rfc` case is the M6 §7.3/§8 kernel-side networking gap, not the browser:
its fetch completed in M6 (hence `BadEncoding`), but in M7 the flaky guest
virtio-net dropped the connection mid-transfer (`fetch_curl_done: Unknown cURL
response code 56`, `CURLE_RECV_ERROR`), the same class as iana.org. The charset
fix is validated; the remaining failure is out of M7's "kernel untouched" scope.

The `example.com` screenshot histogram makes the fix visible directly: M6's
shot is dominated by the `#BEBEBE` grey (320 437 px) of NetSurf's
"BadEncoding" error box, while M7's shot swaps that for `#EEEEEE` (229 242 px)
of the page's own light background and gains black text (`320 579 → 333 521` px)
— the "Example Domain" content, rendered.

---

## 3. GIF — refuted (window clipping, not a decode bug)

M6 §5 attributed the missing `#FF8800` GIF to `nsgif_redraw` plotting an unset
`frame_image` for a single-frame GIF. Re-reading the code shows the decode path
is sound:

* `nsgif_redraw` calls `nsgif_get_frame` whenever `current_frame !=
  decoded_frame`; `nsgif_get_frame` runs `gif_decode_frame(gif, 0)` for a
  single-frame GIF (frame 0, `decoded_frame` starts at `GIF_INVALID_FRAME = -1`).
* `gif_decode_frame` clears frame 0, LZW-decodes the image data into
  `frame_image`'s buffer, then calls `bitmap_modified` at `gif_decode_frame_exit`
  — exactly like the PNG/JPEG handlers' redraw path.

The real reason "no `#FF8800`" was observed: the imagetest page's `<img>` rows
are stacked, and the GIF is the 4th row, below the 640×480 JPEG row. The NetSurf
window's content area ends at ~1004 px, so the GIF — and the JPEG — fall below
the fold. The M6 screenshot confirms this: it contains the small green PNG
(`#00CC00`, 256 px at y=833) and the large blue PNG (`#3F6EFF`, 34 400 px at
y=918, clipped to 86 px height), but **no JPEG plasma colours** — the JPEG is
just as absent as the GIF, which the "GIF-specific decode bug" reading cannot
explain.

To make the GIF readable as a decode signal, M7 adds a dedicated `giftest` page
(`netsurf-giftest.html`) that places the same `img-anim.gif` at the top of the
page, above the fold. The matrix run confirms the GIF renders: the `giftest`
screenshot contains **65 651 `#FF8800` pixels** (`nav=2 box=1 redraw=2`) — the
orange single-frame GIF decoded and plotted at full 320×240. No decode fix was
needed.

This is the same misdiagnosis class as M2's "CSS gap" (a crash-loop) and
M3.6/M4's "deferred decode never runs" (it ran on redraw): a screenshot
observation of a *clipped* page read as a format-specific render bug.

---

## 4. CSS layout — re-survey

The M6 §7.4 static ceiling stands: libcss is CSS 2.1-level — no flexbox, CSS
grid, transforms, or `position:sticky` — and JavaScript / WebP / librsvg are
compiled out. That ceiling is not addressable in M7 (it would be a libcss
upgrade, not a fix).

For the CSS the engine *does* implement, the pages that render lay out
correctly: the bundled home page (two `float:left` columns, a `border=1` table,
`<b>`/`<code>`/`<i>` text, the `overflow:hidden` clearfix) renders with the
expected cream `#F4E8D0` / blue `#1A4F8B` / white-card palette, and man7's
static document renders its table/`<pre>`/link styling. No concrete, tractable
CSS-layout defect was found to fix; the remaining "gaps" are the feature ceiling
and the window-fold clipping (§3).

The home page also doubles as the **UTF-8 regression** for §2: it declares
`<meta charset="utf-8">`, so it exercises the internal UTF-8 codec path that
`-DWITHOUT_ICONV_FILTER` switches on. Its M7 histogram matches the M6 one
pixel-for-pixel on the palette colours (cream `#F4E8D0` 79 459 px, blue
`#1A4F8B` 42 545 px — both identical to M6, `box=1 redraw=2`), so the codec
change is a strict no-op for already-working UTF-8 pages.

---

## 5. Remaining items

- **Re-run the 4 timeout sites** (wikipedia, hackernews, csszengarden, cnnlite)
  — M6 §7.3 raised the connect-timeout clamp to 300 s; the sites now fail
  `code 7` (`CURLE_COULDNT_CONNECT`), the kernel-side flaky virtio-net, not the
  browser. Out of scope (kernel untouched).
- **gconv modules for non-HTML iconv users.** `-DWITHOUT_ICONV_FILTER` fixes the
  HTML parser's input filter. `utils/utf8.c` (form submission `utf8_to_enc`,
  view-source `utf8_from_enc`) and glib's `g_convert` still use glibc iconv and
  would need the riscv64 gconv modules bundled (or `utils/utf8.c` ported to the
  internal codec) for non-UTF-8 charsets. Not exercised by the matrix sites.
- **Window-fold clipping.** The imagetest page's lower rows fall below the
  NetSurf window's visible area; a matchbox-wm window-positioning concern, not a
  browser render bug. `giftest` works around it for the GIF.
- JavaScript remains off (needs a riscv64 duktape cross-build).

---

## 6. Artifacts

| file | what it is |
|---|---|
| `tools/riscv/xorg/BROWSER-M7-report.md` | this report |
| `tools/riscv/xorg/build_netsurf.sh` | `-DWITHOUT_ICONV_FILTER` on the cross `CFLAGS` |
| `tools/riscv/xorg/netsurf-giftest.html` | GIF-at-top render test page |
| `tools/riscv/xorg/render_matrix.sh` | `giftest` site added to the matrix |
| `tools/riscv/systemd/build_systemd_desktop.sh` | step 13g bundles the giftest page |
| `/tmp/browser-m7/<site>/shot.png` | per-site screenshot (infocern/example/rfc/home/giftest) |
| `/tmp/browser-m7/<site>/serial.log` | per-site systemd+NetSurf log |
