# DEMO-M1 — a one-click demo of the full Asterinas RISC-V system

Date: 2026-08-15
Status: **MILESTONE ACHIEVED** — one command builds the complete rootfs
(systemd + desktop + nix) and boots it in QEMU, capturing a systemd startup
transcript and a pixel-verified desktop screendump. No kernel changes.

## Objective

Every prior milestone delivered a *piece* (systemd as PID 1, the Xorg desktop
session, the nix profile) behind a separate gate script. This milestone turns
the three into a single, self-contained **demo**: one command that assembles
the whole system, boots it, and leaves behind the two things a viewer needs —
the systemd startup log and the rendered desktop — with no interactive setup.

Concretely it delivers four things:

1. `tools/riscv/demo-all.sh` — the one-click entry point;
2. the demo artifacts (`target/demo/`): an ANSI-stripped boot log and a desktop
   screendump converted from PPM to PNG;
3. `README-DEMO.md` — the architecture diagram + boot flow + one-command usage;
4. this report.

## Deliverables

| File | Purpose |
|---|---|
| `tools/riscv/demo-all.sh` | build rootfs → re-pack boot disk → boot → capture + convert |
| `tools/riscv/systemd/boot_systemd_nixos.py` | +`--settle-seconds` (see §Screenshot timing) |
| `README-DEMO.md` | architecture diagram, boot flow, one-command usage |
| `tools/riscv/systemd/DEMO-M1-report.md` | this report |
| `target/demo/systemd-boot.log` | ANSI-stripped systemd startup transcript |
| `target/demo/asterinas-desktop.png` / `.ppm` | rendered desktop screendump |

## The assembly (unchanged from NIXOS-STAGE2-M1)

The demo does not reimplement any of the assembly — it reuses the milestone
scripts as the single build step:

```
demo-all.sh
  └─ build_systemd_desktop_nix.sh
       ├─ build_systemd_desktop.sh --no-pack     # systemd base + desktop payload
       └─ layering: nix profile + musl closure    # /nix/store + /nix/var/nix/profiles
       └─ pack as raw newc cpio (no gzip)
  └─ re-pack boot.ext4 (kernel Image + initramfs + qemu-virt.dtb)
  └─ boot_systemd_nixos.py  (U-Boot booti + bochs framebuffer chain)
  └─ post-process: strip ANSI, PPM → PNG
```

The only genuinely new logic is the **orchestration** (prerequisite checks,
the boot-disk re-pack, artifact archiving) and the **screenshot timing**.

## Boot flow

The `boot_systemd_nixos.py` driver walks the same chain every systemd milestone
has used: QEMU `-machine virt` → U-Boot `booti` (with a `simple-framebuffer`
DTB node injected for the bochs display) → the Sv39 kernel → `/init` → systemd
(PID 1) → `graphical.target`. The demo passes custom `--screenshot` and
`--serial-log` paths so the artifacts land in `target/demo/`, and a
`--settle-seconds` so the screendump is taken *after* the desktop renders.

## Verification

A full `demo-all.sh` run (build + boot + settle + convert) produced:

```
=== NIXOS-STAGE2-M1 result ===
  init-launcher: OK       nix-activation: OK
  systemd-banner: OK      nix-hello:      OK
  basic-target: OK        nix-nixos-info: OK
  multi-user-target: OK   nix-jq:         OK
  graphical-target: OK    nix-curl:       OK
  xorg-started: OK        xorg-input-devices: OK
  matchbox/xpanel/pcmanfm/xterm-started: OK
  collection-ended: desktop-up
```

Pixel analysis of the 1280×1024 screendump matches the desktop layout from
`SYSTEMD-DESKTOP-M1` exactly:

| color | share | what it is |
|---|---|---|
| `#ffffff` | 88.0% | xterm / pcmanfm window backgrounds |
| `#202028` | 3.8% | xpanel bar |
| `#dcdad5` | 2.9% | GTK2 client content (pcmanfm) |
| `#496179` | 1.5% | matchbox titlebar |
| `#697d96` / `#384961` | ~0.2% | matchbox frame edges |

The demo artifacts are `target/demo/systemd-boot.log` (22 KB, zero ANSI escape
bytes) and `target/demo/asterinas-desktop.png` (8-bit/color RGB).

## Screenshot timing — the one real fix in this milestone

The boot driver's collection loop finishes the moment systemd reports
`graphical.target` **and** Xorg adds its first input device ("Adding extended
input device keyboard", ~40 s into boot). The session clients — which systemd
has already `Started` but which are blocked in `XOpenDisplay` until Xorg is
ready — connect and render *after* that. The first demo screendump (6 s later)
was a **fully black** framebuffer (`histogram: 1,310,720 × #000000`).

Fix: `--settle-seconds` (default **60 s**) on `boot_systemd_nixos.py` sleeps
after the collection loop ends, before the `screendump`, giving matchbox +
xpanel + pcmanfm + xterm time to connect, map, and draw. 60 s is comfortably
past the render point on this machine; the option exists so slower runs can
bump it. This is a *demonstration-only* concern — the milestone *result* (all
markers OK) is unaffected, only the *picture* was early.

## Post-processing notes

- **ANSI stripping.** systemd emits both classic CSI (`[0;1;32m`) and colon-style
  color codes (`[38:5:185m`), plus private-mode (`[?25h`), OSC (`]104…BEL`), and
  DECSTR (`[!p`) sequences. The demo strips all of them so the log is plain text.
- **PPM → PNG.** done with ImageMagick (`magick`, falling back to `convert`);
  both are kept (`target/` is git-ignored, so these are local evidence, not
  repo content).

## Gap list (all inherited — none block the demo)

| Symptom | Root cause | Owner |
|---|---|---|
| `memory.max` I/O error once per service | cgroup-v2 `memory.max` read-only in this tree | kernel (session A) |
| `Failed to start device monitor: Protocol not available` | AF_NETLINK unimplemented | kernel (session A) |
| `Unimplemented syscall 258/293/219` (`riscv_hwprobe`/`rseq`/…) | glibc/musl startup probes | kernel (future) |
| `FBIOBLANK: Invalid argument` | fbdev blanking ioctl unsupported | kernel (future) |
| `unsupported wait options: WEXITED` | waitid() option subset | kernel (future) |

## Reproduce

```bash
tools/riscv/demo-all.sh              # build + boot + screenshot (the whole demo)
# boot log:        target/demo/systemd-boot.log
# desktop render:  target/demo/asterinas-desktop.png
```

Preconditions are the same as `gate_nixos.sh`: a Sv39 kernel Image, U-Boot +
boot disk, the systemd/desktop cross-build, and the nix products in the sibling
tree. The kernel is **not** rebuilt.

## Next steps

1. **Interactive window.** `demo-all.sh` is headless by design (that is what
   makes it a *scriptable* demo). A `--display-gtk` variant that opens a QEMU
   window for a live, clickable session is a natural follow-up.
2. **Content-hashed store** and a **nix-managed daemon as a unit** (inherited
   from NIXOS-STAGE2-M1) would deepen the "real NixOS" story beyond referencing
   the profile.
3. **CI.** Folding `demo-all.sh` into a workflow would turn the demo into a
   regression gate (fail if any marker is missing).
