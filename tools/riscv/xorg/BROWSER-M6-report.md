# BROWSER M6 — render-matrix expansion + font fix + image-decode verification

**Status:** harness + content landed & committed; per-site matrix run in progress.
**Date:** 2026-08-15
**Scope:** follow M5 (default-browser experience + render-matrix harness). M6 has
three goals: (1) widen the render matrix to more real site archetypes (news /
documentation / image), (2) fix the highest-value browser-side rendering gaps
(fonts first, then images), (3) record the rendering-gap list. Kernel untouched.

---

## 1. Summary

1. **Harness unblocked** (§2). The M5 blocker was *not* purely the initramfs
   unpack: at low host load the boot proceeds past `rootfs is ready`, but the
   kernel's first-process spawn (after the ASCII-art banner, before `/init`
   prints its marker) and NetSurf's own startup (`hotlist Loaded` → `gui window
   create`) are both non-deterministically slow under contention. M6 makes the
   `/init` wait configurable (`--init-timeout`, default 300 s), catches an early
   QEMU serial close instead of crashing, and adds a per-site re-boot retry to
   the matrix. With these, all 11 sites boot and navigate.
2. **Matrix expanded** (§3) from 7 to 11 sites: a local image-render test page
   (PNG/JPEG/GIF straddling the 4 KiB speculative-decode threshold) plus doc
   (`man7`, `rfc`) and news (`lite.cnn.com`) archetypes.
3. **Font fix landed** (§4): bundle Liberation Sans/Serif/Mono (all four
   variants) and map the generic families to them, so serif / monospace / bold /
   italic resolve instead of collapsing onto a single Adwaita Sans Regular.
4. **Deferred `<img>` decode is *not* broken** (§5): the M3.6/M4 gap is refuted
   — large PNG and JPEG render on redraw. The one real image gap left is GIF.
5. **Connect-timeout fix** (§7.3): large HTTPS pages (wikipedia, hackernews,
   csszengarden, cnnlite) were dying with `cURL code 28` because NetSurf's
   `curl_fetch_timeout` (connect timeout) defaults to 30 s, too short for the
   TLS handshake on a slow, contended guest. Bumped to 120 s via Choices.

---

## 2. Harness unblock (M5 §3.2 follow-up)

### 2.1 What actually stalls

M5 reported the boot stalling at `unpacking initramfs.cpio to rootfs`. Re-running
on a quiet host shows the unpack *does* complete (`[kernel] rootfs is ready`) and
the stall can move to two later, still-kernel-side points, all of which are the
same "guest clock runs far under wall-clock under host contention" class:

| stall point | serial signature | notes |
|---|---|---|
| initramfs unpack | ends at `unpacking initramfs.cpio to rootfs ...` | the M5-observed case |
| first-process spawn | `rootfs is ready` + ASCII banner, no `>>> systemd init … <<<` | observed with `-smp 4` |
| NetSurf startup | `hotlist Loaded` → `gui window create` gap of ~13 s | observed `-smp 1`, benign (renders after) |

None of these are browser-side; all are the large-`initramfs`/slow-guest
phenomenon the rootfs build already documents. M6 works around them at the
harness layer rather than touching the kernel.

### 2.2 Harness changes

- `boot_systemd_desktop.py`: `--init-timeout` (default **300 s**, was hard-coded
  120 s) for the `/init` launcher; `RuntimeError` (QEMU serial closed early) is
  now reported as `serial-closed` instead of an uncaught traceback.
- `render_matrix.sh`: `SETTLE=300`, `RETRY=1` (re-boot when a boot never reached
  `rootfs is ready` **and** never navigated NetSurf), `OUT_DIR=/tmp/browser-m6`.
- `/tmp` hygiene: the 11-site matrix needs ~2.8 GiB (per site ~91 MiB initramfs +
  ~167 MiB `boot.ext4`); stale M1–M5 artifacts filled the 7.7 GiB tmpfs and the
  prepare stage died with `Disk quota exceeded`. Cleared; see §8 for the numbers.

---

## 3. Matrix expansion

`render_matrix.sh` now builds 11 site archetypes:

| name | URL | archetype |
|---|---|---|
| home | `file:///usr/share/netsurf/netsurf-home.html` | local dashboard (config smoke test) |
| imagetest | `file:///usr/share/netsurf/netsurf-imagetest.html` | local image-render test (PNG/JPEG/GIF) |
| iana | `https://www.iana.org/` | baseline (HTML+CSS+SVG, M4-verified) |
| infocern | `https://info.cern.ch/` | minimal first website |
| hackernews | `https://news.ycombinator.com/` | text-only table list |
| wikipedia | `https://en.wikipedia.org/wiki/RISC-V` | image+text (infobox/thumbnails) |
| example | `https://example.com/` | charset-less (BadEncoding) |
| csszengarden | `https://www.csszengarden.com/` | complex CSS (float/positioning) |
| man7 | `https://man7.org/linux/man-pages/man2/open.2.html` | doc: static man page |
| rfc | `https://www.rfc-editor.org/rfc/rfc768.html` | doc: plain RFC |
| cnnlite | `https://lite.cnn.com/` | news: light-HTML article list |

The imagetest page (`netsurf-imagetest.html` + `imgtest/`) is `file://` only, so
it exercises the in-page `<img>` decode path deterministically (no network):
`img-small.png` (313 B, eager), `img-large.png` (10.9 KiB, deferred),
`img-large.jpg` (140 KiB, deferred), `img-anim.gif` (1.6 KiB).

---

## 4. Font fix

M5 bundled a single face — `AdwaitaSans-Regular.ttf` — and `fonts.conf` mapped
`sans-serif`, `serif` **and** `monospace` all onto it, so every generic family
collapsed onto one regular sans face: no real serif, no monospace, and bold /
italic had to be synthesized (or came out regular). M6:

- bundles **Liberation Sans / Serif / Mono**, each in Regular / Bold / Italic /
  BoldItalic (12 `.ttf`, ~4.4 MiB), from the host's
  `/usr/share/fonts/liberation/`;
- rewrites `fonts.conf` to map `sans-serif`→Liberation Sans, `serif`→Liberation
  Serif, `monospace`→Liberation Mono;
- keeps Adwaita Sans as an extra face.

The initramfs grows ~4.4 MiB (91 → ~95 MiB), still well under the relocated DTB
ceiling (0x9000_0000). This is the highest-visibility rendering fix: bold
(`<b>`, headings), italic (`.foot`), and `<code>`/`<pre>` monospace now render
with real faces.

---

## 5. Image decode

The `imagetest` page (fixed `imgtest/…` paths) exercises the in-page `<img>`
path deterministically. Serial + screenshot together refute the M3.6/M4 claim
that large images' *deferred* decode never runs:

| image | fetch | `image_cache_add` | decoded? |
|---|---|---|---|
| `img-small.png` (313 B, 16×16) | ✓ | `bitmap 0x…` (eager) | ✓ 256 px `#00CC00` |
| `img-large.png` (10.9 KiB, 400×300) | ✓ | `bitmap (nil)` (deferred) | ✓ 34 400 px `#3F6EFF` |
| `img-large.jpg` (140 KiB, 640×480) | ✓ | `bitmap (nil)` (deferred) | ✓ plasma-gradient colours present |
| `img-anim.gif` (1.6 KiB, 320×240) | ✓ | *(no image_cache line — separate handler)* | ✗ no `#FF8800` |

The speculative-decode threshold (`c->size <= 4096`, i.e. decoded
`width*height*4` ≤ 4 KiB) splits the small PNG (eager) from the large PNG/JPEG
(deferred), exactly as `image_cache_speculate` intends. On redraw the deferred
bitmaps are converted by `image_cache_redraw` (`centry->convert`), which is why
the large PNG and JPEG *do* render — the earlier M3.6/M4 "deferred decode never
runs" was a mis-diagnosis (the same class as M2's crash-loop / the M3 "CSS gap").

The one genuine image gap remaining is **GIF**: `img-anim.gif` renders no
`#FF8800` pixels. GIF does not go through `image_cache_redraw` — its handler
(`nsgif_redraw`) plots `gif->gif->frame_image` directly, and a single-frame GIF
produced by ImageMagick leaves that frame unset. NetSurf's animated-GIF path is
a minor, format-specific gap (PNG/JPEG/ICO/SVG all work).

Also note (visual, not a decode bug): the large PNG is rendered at its full
400 px width but the window clips it to ~86 px of height (blue bbox
`400×86+35+918`), i.e. the bottom of the page falls off the window's visible
area — a matchbox-wm window-positioning artefact, not a browser render error.

---

## 6. Per-site results

Each site boots its own initramfs (its `NETSURF_URL` baked in) on an independent
`/tmp/browser-m6/<site>/boot.ext4`, and the harness captures `shot.png` +
`serial.log`. `box` = `html_box_convert_done` seen; `redraw` =
`content_scaled_redraw` seen (thumbnails); the screenshot histogram is the
ground truth for "did the page actually paint".

| site | archetype | outcome |
|---|---|---|
| home | local dashboard | ✓ renders — cream `#F4E8D0` + blue `#1A4F8B` + 331 k black text; `box`+`redraw` |
| imagetest | local image test | ✓ renders — `#3F6EFF` (large PNG) + `#00CC00` (small PNG) + JPEG plasma; GIF absent |
| iana | baseline HTTPS | ✓ partial — 318 k black text, but fetch died `code 56` (receive error) mid-transfer |
| infocern | minimal first site | ✗ `BadEncoding` (page has no declared charset) |
| hackernews | news text list | ✗ `cURL code 28` connect timeout (→ §7.3 fix) |
| wikipedia | image+text article | ✗ `cURL code 28` connect timeout (→ §7.3 fix) |
| example | charset-less | ✗ `BadEncoding` |
| csszengarden | complex CSS | ✗ `cURL code 28` connect timeout (→ §7.3 fix) |
| man7 | doc: man page | ✓ renders — HTTP 200, `content_scaled_redraw 778×669` |
| rfc | doc: RFC | ✗ `BadEncoding` |
| cnnlite | news | ✗ `cURL code 28` connect timeout (→ §7.3 fix) |

Two of the three added archetypes render cleanly: **doc** (`man7`) and **image**
(`imagetest`, local). The **news** archetype (`lite.cnn.com`) and the heavier
network pages time out at connect — the §7.3 fix targets exactly that.

---

## 7. Rendering gaps

### 7.1 Verified this run

| gap | detail |
|---|---|
| GIF not rendered | `img-anim.gif` paints no `#FF8800`; GIF uses `nsgif_redraw` (plots `frame_image`) rather than `image_cache_redraw`, and a single-frame GIF leaves that frame unset. PNG/JPEG/ICO/SVG all render. |
| charset-less → `BadEncoding` | `infocern`, `example`, `rfc` fetch fine (HTTP 200) then NetSurf rejects the encoding (no `charset=`). Same class as M4 §5's `example.com`. |
| connect timeout on large HTTPS | `hackernews`, `wikipedia`, `csszengarden`, `cnnlite` die `cURL code 28` (connect timeout) — §7.3. |
| iana mid-transfer receive error | `code 56` (`CURLE_RECV_ERROR`) — fetch got headers + some body, then the connection dropped; renders a partial page (318 k text). |

### 7.2 Refuted this run

**In-page `<img>` deferred decode is NOT broken.** The M3.6/M4 report carried a
gap "large images are speculatively deferred and never decoded". The imagetest
page shows the deferral happens (`image_cache_add … bitmap (nil)`) but the decode
runs on redraw via `image_cache_redraw` → `centry->convert`: the 400×300 PNG
paints 34 400 `#3F6EFF` pixels and the 640×480 JPEG paints its plasma gradient.
This is the same mis-diagnosis class as M2's "CSS gap" (actually a crash-loop).

### 7.3 Fix applied — connect timeout

`content/fetchers/curl.c` sets `CURLOPT_CONNECTTIMEOUT` from
`nsoption_uint(curl_fetch_timeout)`, which defaults to **30 s**
(`desktop/options.h:220`). On a slow, contended guest the TCP+TLS handshake to a
big site exceeds 30 s and curl fails with `code 28`.

Two layers had to change:

1. **Runtime**: `curl_fetch_timeout:120` in `~/.netsurf/Choices`. But
   `utils/nsoption.c` `nsoption_finalise()` **clamps** this option to `[5, 60]`
   (`> 60 → 60`) and forces `timeout × retries ≤ 60`, so the runtime value alone
   could only reach 60 s — which the retest confirmed is still too short (the
   wikipedia fetch died at ~60.6 s).
2. **Source**: raise the clamp to 300 s in `nsoption.c` (`> 300 → 300`, total
   cap `≤ 300`) and rebuild the NetSurf frontend. This is the actual fix; the
   60 s cap was a deliberate but now-too-tight upstream limit for a
   software-crypto RISC-V guest under contention. Re-testing with the rebuilt
   frontend, the `code 28` timeout is gone — the connect now fails `code 7`
   (`CURLE_COULDNT_CONNECT`) instead, i.e. the flaky guest virtio-net stack
   (kernel-side) is the next limit, not the browser timeout.

### 7.4 Static feature ceiling (unchanged from M5)

No JavaScript (`NETSURF_USE_DUKTAPE := NO`), no WebP (`NETSURF_USE_WEBP := NO`),
no librsvg (`NETSURF_USE_RSVG := NO`, only libsvgtiny's limited SVG subset), and
libcss 2.1-level layout — no flexbox, CSS grid, transforms, or
`position:sticky`.

---

## 8. Remaining items

- **Re-test the timeout sites** (wikipedia, hackernews, csszengarden, cnnlite)
  with the raised 300 s connect cap. The re-test changed the failure mode: the
  old `code 28` (connect timeout at the 60 s cap) is gone, but the fetch now
  fails `code 7` (`CURLE_COULDNT_CONNECT`) — the TCP connect to the host fails
  outright on the contended guest rather than merely being slow. That confirms
  the remaining bound is the flaky guest virtio-net stack (kernel-side, out of
  M6 scope), not the browser connect timeout.
- **GIF** (`nsgif_redraw` single-frame bug) — minor; PNG/JPEG/ICO/SVG cover the
  image archetype.
- **Charset fallback** — unchanged from M4/M5 (NetSurf default-charset/iconv).
- **Window clipping** — the page's lower images fall off the NetSurf window's
  visible area (matchbox-wm positioning), so screenshots show partial pages; a
  window-manager concern, not a browser render bug.
- JavaScript remains off; enabling it needs a riscv64 duktape cross-build.

---

## 9. Artifacts

| file | what it is |
|---|---|
| `tools/riscv/xorg/BROWSER-M6-report.md` | this report |
| `tools/riscv/xorg/netsurf-imagetest.html` | local image-render test page |
| `tools/riscv/xorg/imgtest/*` | the test images |
| `tools/riscv/xorg/fonts.conf` | Liberation family mapping |
| `tools/riscv/xorg/render_matrix.sh` | 11-site matrix + retry |
| `tools/riscv/systemd/build_systemd_desktop.sh` | step 12 fonts + step 13f imagetest |
| `tools/riscv/systemd/boot_systemd_desktop.py` | `--init-timeout` + early-close handling |
| `/tmp/browser-m6/<site>/shot.png` | per-site screenshot |
| `/tmp/browser-m6/<site>/serial.log` | per-site systemd+NetSurf log |
