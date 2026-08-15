# BROWSER M4 — the kernel-networking gap is fixed: HTTPS fetch + NetSurf render a real site

**Status:** milestone reached — the virtio-net MMIO packet path now moves
packets end-to-end (DNS UDP → TCP → TLS → HTTPS). `netsurf-gtk` fetches and
renders a real HTTPS website on the systemd desktop.
**Date:** 2026-08-15
**Scope:** follow the M3 blocker into the kernel, find why the virtio-net device
registered but moved no packets, fix it, and verify with a real HTTPS render.

---

## 1. Summary

M3 (§11 in `BROWSER-M1-report.md`) ended at a *kernel* blocker: the curl+OpenSSL
browser stack was verified active at runtime, but `https://example.com/` died in
the kernel — DNS (UDP) returned `CURLE_COULDNT_RESOLVE_HOST` and, with
`/etc/hosts` bypassing DNS, TCP connect timed out. U-Boot's own virtio-net driver
had already done DHCP over the same device, so the hardware + slirp path was
proven good and the gap was isolated to the kernel's virtio-MMIO network path.

This milestone found and fixed the root cause in the kernel: **the virtio-net
header struct was 12 bytes when it should be 10**, shifting every TX frame by two
bytes so the device read the trailing two zero bytes as the start of the frame
(corrupting the destination MAC, so every frame was dropped), and offsetting every
RX payload by two bytes.

After the fix, the full stack works: DNS resolution, TCP connect, the TLS
handshake, and the HTTP response all complete, and NetSurf renders
`https://www.iana.org/` (HTML + CSS + favicon + SVG logo) on the desktop.

---

## 2. Diagnosis: what "blocked by kernel networking" actually meant

The M3 report had already isolated the symptom to the kernel, but not the
*mechanism*. This milestone instrumented the virtio-net driver (RX/TX/notify/IRQ
logging, plus a `--loglevel` boot-flag in `boot_systemd_desktop.py`) and re-ran
the M3 boot on an independent `/tmp` disk. The resulting serial trace shows the
device is fully functional — it receives packets and raises interrupts — but the
**egress path sends ARP requests that get no reply**:

```
[57.038] virtio: send packet, token = 0, len = 42      ← ARP request for 10.0.2.3
[57.041] virtio: notify send queue: sent 1 packets
[57.042] virtio: virtio-net: send queue interrupt      ← device consumed the TX buffer
[57.042] virtio: virtio-net: recv queue interrupt
        (… no `receive packet` follows — no ARP reply …)
[58.334] virtio: send packet, token = 0, len = 66      ← TCP SYN retransmits, also unanswered
```

The `send queue interrupt` is the key clue: QEMU *did* consume the TX buffer (the
used-ring was updated), so the frame reached the device — yet no reply came back.
That rules out "the notify/queue kick is missing" and points at the *content* of
the transmitted frame: it was malformed.

### 2.1 Root cause: a 12-byte virtio-net header where the spec demands 10

`VirtioNetHdr` (`kernel/comps/virtio/src/device/network/header.rs`) declared an
unconditional `num_buffers: u16` at the end:

```rust
pub(super) struct VirtioNetHdr {
    flags: Flags,          // 1
    gso_type: u8,          // 1
    hdr_len: u16,          // 2
    gso_size: u16,         // 2
    csum_start: u16,       // 2
    csum_offset: u16,      // 2
    num_buffers: u16,      // 2  ← should be absent
}                          // total 12 bytes, spec says 10
```

Per the virtio-net spec, `num_buffers` is present **only** when
`VIRTIO_NET_F_MRG_RXBUF` is negotiated. This driver never negotiates it —
`NetworkFeatures::supported_features()` is only `VIRTIO_NET_F_MAC |
VIRTIO_NET_F_STATUS` (see `config.rs`). So the header must be the plain 10-byte
`virtio_net_hdr`.

The two extra bytes break both directions:

- **TX** — `TxBuffer::new` prepends the 12-byte header before the frame. The
  device reads a 10-byte header, so the trailing two zero bytes (`num_buffers`)
  become the first two bytes of the Ethernet frame — i.e. the destination MAC's
  first two bytes. An ARP broadcast (`ff:ff:ff:ff:ff:ff`) became
  `00:00:ff:ff:ff:ff`, no longer broadcast, so slirp dropped it. Every TX frame
  was silently corrupted this way.
- **RX** — `receive()` computes `payload_len = used_len - size_of::<VirtioNetHdr>()`
  with the header as 12 bytes, but QEMU writes a 10-byte header, so the payload
  was misaligned (skipped the first two frame bytes).

This is exactly why the device "registered and initialized" (everything up to the
header was correct) yet "moved no packets" — a classic off-by-header-size bug that
no amount of interrupt/queue debugging would surface.

### 2.2 The fix

Remove `num_buffers` from `VirtioNetHdr`, leaving the correct 10-byte
`virtio_net_hdr`:

```
fix(virtio-net): use 10-byte virtio_net_hdr (drop num_buffers)    (5dccbd20d)
```

---

## 3. Verification

### 3.1 Boot-verify infrastructure (unchanged from M3, reused)

The shared `target/qemu-uboot/current/boot.ext4` is in use by the VNC QEMU, so all
verification boots from independent `/tmp` disks (`--boot-disk`). The kernel is
rebuilt with the committed fix and repacked into the boot disk alongside the M3
desktop initramfs (systemd + desktop + NetSurf + glibc resolver + CA bundle +
`resolv.conf → 10.0.2.3`). Two supporting commits made the diagnosis repeatable:

```
test(net): add --loglevel boot flag, quiet syscall tracing, log net IRQs (e571d20c8)
```

### 3.2 curl https pulls a real page

With `--net --loglevel info`, the fixed kernel shows the full packet ladder that
was absent before the fix — ARP → DNS → TCP → TLS → HTTP:

```
[57.038] send packet, len = 42    ← ARP request (DNS server)
[57.051] receive packet, len = 74 ← ARP reply
[57.089] send packet, len = 74    ← DNS query (UDP)
[57.094] receive packet, len = 100← DNS response
[57.634] send packet, len = 42    ← ARP request (target host)
[57.642] receive packet, len = 74 ← ARP reply
[58.334] send packet, len = 66    ← TCP SYN
[62.390] receive packet, len = 113
[63.763] send packet, len = 571   ← TLS ClientHello
[63.786] receive packet, len = 1504 ← TLS ServerHello/cert (full MTU segments)
[63.790] receive packet, len = 1400
```

NetSurf's curl fetcher then reports success — no `CURLE_*` error:

```
(37.262) content/fetchers/curl.c:1083 fetch_curl_done: done https://example.com/
```

Every layer that M3 showed as broken (UDP DNS, TCP connect, TLS) now completes.

### 3.3 NetSurf opens a real website

`example.com` currently serves `Content-Type: text/html` with **no charset** (and
no `<meta charset>`), which trips NetSurf's charset fallback (`BadEncoding`) — a
browser-side quirk unrelated to the kernel. Pointing the boot at a site that
declares UTF-8 (`https://www.iana.org/`) gives a clean end-to-end render:

```
(30.829) fetch_curl_done: done https://www.iana.org/
(35.166) fetch_curl_done: done https://www.iana.org/static/css/iana_website…css
(35.739) fetch_curl_done: done https://www.iana.org/static/img/bookmark_icon…ico
(35.935) html_box_convert_done: Done XML to box (0x2aac5267f0)
(41.345) browser_window_history_update: Updating history entry for
         "Internet Assigned Numbers Authority"
(40.313) fetch_curl_done: done https://www.iana.org/static/img/iana-logo-homepage…svg
(41.365) content_scaled_redraw: Content 0x2aac5267f0 1024x881
```

The page title is resolved, the HTML→box conversion completes, the CSS/favicon/SVG
sub-resources are fetched over HTTPS, and the content is redrawn at 1024×881. The
screenshot histogram (`/tmp/m4-iana.png`) confirms a rendered text page: ~347 k
black pixels of text (the M2/M3 local-page baseline was ~13 k), plus the white
page area, `#202028` xpanel, `#DCDAD5` GTK chrome, and `#496179` matchbox
titlebars.

---

## 4. What was and wasn't the problem

| hypothesis (from M3) | verdict |
|---|---|
| virtio-MMIO interrupt routing is broken | ❌ — interrupts fire (the RX softirq ran) |
| virtio-MMIO queue-notify is a no-op | ❌ — `notify` kicks the device; TX buffers are consumed |
| `single_interrupt` ignored breaks networking | ❌ — that warning is benign: MMIO multiplexes all queue IRQs onto one line anyway |
| **the virtio-net header is 2 bytes too long** | ✅ — the actual bug |

The one-line fix (`-num_buffers`) is the entire kernel change. Everything else in
the driver — feature negotiation, queue setup, DMA addresses, interrupt handling,
the smoltcp poll loop — was already correct and is now exercised for the first
time on a real data-moving device.

---

## 5. Remaining items (browser-side, out of kernel scope)

- **Charset fallback.** Pages without a declared charset (`Content-Type: text/html`
  with no `charset=`, e.g. `example.com`) hit NetSurf's `BadEncoding`. The kernel
  delivers the bytes; this is a NetSurf default-charset/iconv concern.
- **In-page `<img>` deferred decode** (from M3.6) is unchanged and still pending.
- **No JavaScript** (`NETSURF_USE_DUKTAPE := NO`) — unchanged.

---

## 6. Artifacts

| file | what it is |
|---|---|
| `tools/riscv/xorg/BROWSER-M4-report.md` | this report |
| `kernel/comps/virtio/src/device/network/header.rs` | the fix (10-byte `virtio_net_hdr`) |
| `kernel/comps/virtio/src/device/network/device.rs` | RX/TX queue IRQ `info!` logging |
| `kernel/src/syscall/mod.rs` | syscall entry tracing moved Info→Debug |
| `tools/riscv/systemd/boot_systemd_desktop.py` | `--loglevel` boot-args flag |
| `/tmp/m4-diag-fixed.log` | fixed-kernel packet ladder (DNS→TCP→TLS→HTTP) |
| `/tmp/m4-diag-iana.log` | NetSurf HTTPS render of `https://www.iana.org/` |
| `/tmp/m4-iana.png` | the milestone screenshot |
