# BROWSER M10 — interactive-desktop verification, render-matrix expansion, and slirp→bridge evaluation

**Status:** GTK desktop verified crash-free; render matrix re-harnessed over an
expanded static-friendly site set with per-site pixel validation; slirp→bridge
switch evaluated (medium cost → deferred).
**Date:** 2026-08-16
**Scope:** four follow-ups on top of M9 (interactive boot-disk repack, PR #48):
(1) close the loop on the interactive GTK desktop by re-running
`boot_gtk_interactive.py` against the fixed initramfs and confirming the
`netsurf.service` crash-loop is gone; (2) expand the render matrix with more
real-world static-friendly sites and re-run it through the `net_validate.sh`
harness with framebuffer pixel validation; (3) evaluate the slirp→bridge switch
flagged in M8 §5; (4) this report.

---

## 1. Summary

1. **Interactive GTK desktop is clean.** Re-running `boot_gtk_interactive.py`
   (which now re-packs its boot disk from the current initramfs, the M9 fix) boots
   the desktop and NetSurf navigates + renders the bundled home page with **zero**
   `netsurf.service` exits/restarts (§2). The resident GTK window is left up.
2. **Render matrix re-harnessed.** A new `render_matrix_net.sh` builds the base
   rootfs once and produces each site's initramfs by only rewriting
   `/etc/netsurf.conf` + re-packing the raw cpio (~0.2 s/site, vs the ~2–3 min
   full rebuild the M6 `render_matrix.sh` paid per site), then boots + fetch-scores
   each site through `net_validate.sh` and pixel-validates the framebuffer dump via
   a new `pixel_validate.py` (§3). The site set is re-curated around what this host
   can actually reach (§3.2), and a `net_validate.sh` scoring bug that let a
   secondary `google.com/favicon.ico` code7 mask a successful main-page http200 is
   fixed (§3.4).
3. **slirp→bridge is not low-cost; deferred.** Switching `-netdev user` → a host
   bridge needs (a) root tap+bridge+NAT setup, (b) a `--tap` boot mode, and (c) a
   `resolv.conf` DNS repoint — and a *true* L2 bridge would additionally need a
   DHCP client or a parameterized guest IP the kernel does not have (§4). It is
   documented with a concrete recipe, not implemented.

---

## 2. Interactive GTK desktop verification (subtask 1)

M9 (PR #48) fixed `boot_gtk_interactive.py` to re-pack `/tmp/vnc-demo/boot.ext4`
from the current kernel + initramfs + DTB on every launch, but the previous
session's *live* GTK guest was still the stale pre-fix process (restart counter
climbing past 275). This milestone kills that process and re-runs the fixed driver.

**Before:** the stale `/tmp/qemu-gtk.log` (pre-fix process) showed
`netsurf.service: Scheduled restart job, restart counter is at 275` and climbing.

**After** (fresh run, `/tmp/qemu-gtk-m10.log`, `-smp 4 -display gtk`):

| check | result |
|---|---|
| `netsurf.service: Main process exited` | **0** |
| `netsurf.service: Scheduled restart` (counter) | **0** |
| `Unable to find resource accelerators` | absent |
| `browser_window_navigate` (home page) | yes |
| `html_box_convert_done` | yes |
| `content_scaled_redraw` | yes (186×160) |

NetSurf navigates `file:///usr/share/netsurf/netsurf-home.html`, converts it to a
box tree, and redraws — the exact resource-walk failure point that crash-looped the
stale binary is cleared, and the unit stays running. The QEMU process (PID
`-display gtk`) is left resident as requested.

---

## 3. Render matrix (subtask 2)

### 3.1 Harness

- **`tools/riscv/xorg/render_matrix_net.sh`** — new. One base rootfs build
  (`build_systemd_desktop.sh --no-pack`), then per-site initramfs = rewrite
  `/etc/netsurf.conf` + `cpio -o` re-pack. Each site boots via `net_validate.sh`
  (re-pack independent boot disk → boot → score fetch outcome), then the framebuffer
  screenshot is pixel-validated. `PARALLEL` / `SETTLE` / `OUT_DIR` are env-tunable;
  site names can be passed as args for a subset.
- **`tools/riscv/xorg/pixel_validate.py`** — new. Scores a P6 PPM into `rendered`
  (≥40 distinct quantized colors — antialiased text + page background), `empty-root`
  (Xorg's uniform gray weave, a handful of colors — the code7 fetch-failure state),
  or `black` (nothing up). Calibrated against the M8 captures: a rendered desktop
  reads `distinct=213 white=41%`, a code7 failure reads `distinct=25`.

### 3.2 Site set

The M6 set is re-curated against what **this host's network** can reach (§3.4).
`wikipedia`, `hackernews`, and `lite.duckduckgo.com` are dropped (they time out from
the host — both IPv4 and IPv6 — so a guest boot against them always reads code7 for
a reason unrelated to the kernel/slirp), and five reachable static-friendly hosts
are added to cover the same archetypes: `wiki.archlinux.org` (wiki), `www.w3.org`
(standards), `suckless.org`, `www.openwall.com` (security; `iso-8859-1`), and the
text-only/news hosts are already covered by `text.npr.org` + `lite.cnn.com`. The
final set is 17 sites: 2 local `file://` pages + 15 real hosts.

### 3.3 Results

17 sites, `PARALLEL=1` (one render guest at a time, alongside the two resident
VNC + GTK guests), `SETTLE=220 s`. 16/17 fetched and rendered cleanly; one site
(`debian`) hit the non-deterministic slow boot (§3.5) on the first pass and was
re-run.

| site | fetch | pixel (distinct) |
|---|---|---|
| home (`file://`) | redraw | rendered (367) |
| giftest (`file://`) | redraw | rendered (382) |
| iana | http200 | rendered (432) |
| example | http200 | rendered (368) |
| rfc | http200 | rendered (357) |
| cnnlite | http200 | rendered (391) |
| textnpr | http200 | rendered (356) |
| kernel | http200 | rendered (430) |
| ietf | http200 | rendered (366) |
| gnu | http200 | rendered (376) |
| openbsd | http200 | rendered (385) |
| debian | http200 | rendered (254) — retry; see §3.5 |
| freebsd | http200 | rendered (254) |
| wikiarchlinux | http200 | rendered (397) |
| w3 | http200 | rendered (370) |
| suckless | http200 | rendered (376) |
| openwall | http200 | rendered (440) |

Every success pairs an `http200`/`redraw` fetch score with a `rendered` pixel
verdict (254–440 distinct quantized colors — antialiased text + page background),
and the fetch score reflects the *main* page (§3.4), so a success is a real
success rather than a favicon-sidecar.

### 3.4 Host-unreachability and the `net_validate.sh` scoring fix

While curating the site list, a host-level preflight (`curl` from the machine that
runs QEMU) revealed three of the M6/M8 sites **never connect from this host** —
`news.ycombinator.com`, `en.wikipedia.org`, and `lite.duckduckgo.com` time out over
both IPv4 and IPv6 (and `www.google.com` too). This reframes the M6/M8 code7 on
wikipedia/hackernews: it was not (only) slirp contention — those hosts are simply
unreachable from this network.

The same preflight exposed a `net_validate.sh` scoring bug. NetSurf fetches a
default `http://www.google.com/favicon.ico` for any page without a favicon; because
google.com is unreachable here, that fetch code7s on *every* page — local or
remote, successful or not. The old scoring checked `code7` before `http200`, so a
successful main-page fetch was masked by the trailing favicon code7 (the smoke run
scored `iana` — which logged `HTTP status code 200` for `https://www.iana.org/` —
as `code7`). The fix scores the *main* page: it extracts the `browser_window_navigate`
URL, treats `file://` pages as `redraw`/`unknown`, and for remote pages lets an
HTTP 200 from the main fetch win over a later favicon code7.

### 3.5 Non-deterministic slow boot

The first `debian` pass (and the earlier `PARALLEL=2` run's `home`/`iana`/`example`)
never reached Xorg's framebuffer draw within the collect window — a black
screenshot (`black=96%`, `distinct=5`) and an `unknown` score. This is the same
raw-cpio unpack / first-process spawn non-determinism documented in M5/M6/M9, not a
site failure: `debian.org` is reachable from the host and renders `http200` on
retry. The `PARALLEL=2` run was far worse (3 of 4 sites `unknown`), confirming the
two concurrent render guests interfere; `PARALLEL=1` is the reliable matrix
concurrency and still leaves ~1-in-17 boots slow.

---

## 4. slirp→bridge evaluation (subtask 3)

M8 §5 flagged QEMU `-netdev user` (slirp) as the residual source of code7/code35
under host contention — slirp's userspace TCP stack is slow to open its outbound
connection when the host is loaded, which the kernel cannot fix. The suggested
mitigation is `-netdev tap` (host bridge). This milestone evaluates the switch cost.

The guest's network identity is a **hardcoded kernel constant** — there is no DHCP
client. `kernel/src/net/iface/init.rs:115-117`:

```rust
const VIRTIO_ADDRESS: Ipv4Address = Ipv4Address::new(10, 0, 2, 15); // /24
const VIRTIO_GATEWAY: Ipv4Address = Ipv4Address::new(10, 0, 2, 2);
```

A switch therefore has three parts:

| part | change | effort |
|---|---|---|
| host tap+bridge+NAT | `ip tuntap add tap0 mode tap` + bridge + `ip addr add 10.0.2.2/24` + `ip_forward=1` + `iptables -t nat -A POSTROUTING -s 10.0.2.0/24 -j MASQUERADE` (root) | low per-boot, but a root **environmental dependency** |
| boot driver | `-netdev user,id=net0` → `-netdev tap,ifname=tap0,id=net0,script=no,downscript=no` (an opt-in `--tap` mode) | low |
| guest DNS | `resolv.conf` bakes `nameserver 10.0.2.3` (slirp's relay); a bridge has no 10.0.2.3, so repoint to a real resolver | low (build-script change) |

The reason the guest IP can stay 10.0.2.15 is that the host bridge is *private* and
NAT'd (10.0.2.0/24 behind host masquerade). A *true* L2 bridge to the physical LAN —
the guest acquiring a LAN IP — would additionally require either a DHCP client in the
kernel or a boot-parameterized `VIRTIO_ADDRESS`/`VIRTIO_GATEWAY`, which is a
non-trivial kernel change (the constants are compile-time; see the `// FIXME: These
flags are currently hardcoded` note in `init.rs`).

**Verdict: medium cost → defer.** The win is real (host-kernel NAT instead of
slirp's userspace stack removes the contention bottleneck), but it (a) adds a
root-privileged, per-boot host-networking step that does not belong in the current
self-contained headless boot path, and (b) for a true bridge, needs guest IP
parameterization the kernel lacks. Single-guest slirp already yields `http200`
(M8 §4), so the bridge only buys anything in the multi-guest/contention case. The
in-kernel M8 fixes remain the durable work; the bridge is a follow-on milestone,
and the recipe above is the concrete starting point.

---

## 5. Remaining items

- The `netsurf.service` unit still races Xorg's ~50 s bring-up (M9 §5) — cosmetic
  with the fixed binary, but a proper `ExecStartPre` display-ready gate remains.
- The `WEXITED` kernel warning is worth a syscall fix on its own merits (unrelated
  to the browser crash).
- The slirp→bridge switch (§4) as a future milestone, ideally with guest IP
  parameterization or a DHCP client to enable a true L2 bridge.

---

## 6. Artifacts

| file | what it is |
|---|---|
| `tools/riscv/xorg/render_matrix_net.sh` | new fast render-matrix harness (net_validate.sh + pixel validation) |
| `tools/riscv/xorg/pixel_validate.py` | new P6 PPM framebuffer screenshot validator |
| `tools/riscv/xorg/BROWSER-M10-report.md` | this report |
| `/tmp/qemu-gtk-m10.log` | fresh interactive-GTK verification transcript |
| `/tmp/browser-m10/*/` | per-site boot dirs (serial.log, shot.ppm, pixel.out, net_validate.out) |
| `/tmp/browser-m10/results.txt` | per-site `name fetch-outcome` lines |
