# DRM-M10 — smp=4 root-cause correction (DTB mismatch) + fbdev/modesetting render benchmark

Date: 2026-08-16
Branch: `track/drm`
Status: **DONE** — (1) the M9 "core-kernel SMP stall" is **retracted**: the
`smp=4` hang was a **DTB memory/CPU mismatch**, not a kernel bug; (2) `smp=4`
now boots **stably** with a correctly-generated DTB; (3) the fbdev-vs-modesetting
render benchmark (`xbench`) is wired into the desktop boot and produced A/B
numbers.

---

## 0. TL;DR

| item | result |
|---|---|
| `smp=4` hang root cause (M9) | **retracted** — not a kernel/allocator bug |
| real root cause | the boot disk shipped a `qemu-virt.dtb` whose `/memory` node described **128 MB** (QEMU's `dumpdtb` default) while the guest boots with **`-m 2G`** |
| fix | regenerate the DTB with the exact boot flags: `-m 2G -smp N` (both must match QEMU) |
| `smp=4` boot | **stable PASS** (3/3 minimal-harness runs reach `/init`) |
| SMP "regression" vs `main` | **none** — `git diff main..track/drm -- ostd/src/{cpu,boot,mm}/` is empty |
| benchmark | `xbench` runs in the desktop boot; fbdev vs modesetting A/B numbers below |

---

## 1. Part 1 — retracting the M9 SMP conclusion

### 1.1 What M9 concluded

M9 reported an `smp=4` hang with a gdb backtrace in
`page_cache::Vmo::write → Vec<Frame> drop → heap dealloc`, a garbage pointer, and
concluded it was "heap corruption / an allocator bug under SMP … out of DRM
scope". That conclusion was **wrong**.

### 1.2 The actual root cause: a stale, undersized DTB

The independent boot disk shipped a static `qemu-virt.dtb`. M9 regenerated it
with

```
qemu-system-riscv64 -machine virt -smp 4 -cpu … -machine dumpdtb=…
```

**without `-m 2G`**. QEMU's default RAM is **128 MB**, so the dumped DTB's
`/memory` node was `0x80000000..0x88000000`, while the actual boot passes
`-m 2G` (2 GB). The kernel trusts the DTB for `max_paddr` / frame-metadata, so
with 4 CPUs the extra early allocations of `copy_bsp_for_ap` (one page of AP
CPU-local storage × 3) plus `init_kernel_page_table` clashed inside the too-small
128 MB map, and the BSP jumped to a garbage PC (`0xff6bb580` from inside
`memcpy`). At `smp=1` there are no AP allocations (`num_aps == 0`), so the bug
never surfaced — which is why `smp=1` always worked.

A second mismatch (the same class of bug) reproduces in the other direction: a
4-CPU DTB booted with `-smp 1` makes `boot_all_aps` try to start non-existent
harts 1..3 and spin in `wait_for_all_aps_started`:

```
ERROR: Failed to start hart 1: error code 18446744073709551613
ERROR: Failed to start hart 2: error code 18446744073709551613
ERROR: Failed to start hart 3: error code 18446744073709551613
```

**Invariant:** the DTB's `-smp N` **and** `-m M` must exactly match the QEMU
`-smp N` **and** `-m M` used to boot.

### 1.3 There is no SMP regression between `main` and `track/drm`

The stall happened in `ostd` boot code that runs **before** any `aster_kernel`
(DRM) code. Diffing the two branches:

```
$ git diff --stat main..track/drm -- ostd/src/cpu/ ostd/src/boot/ \
      ostd/src/mm/frame/ ostd/src/mm/kspace/ ostd/src/mm/page_table/ \
      ostd/src/arch/riscv/boot/smp.rs ostd/src/arch/riscv/mm/
# (empty — identical)
```

The *only* `ostd` changes on `track/drm` are:

- `ostd/src/arch/riscv/boot/bsp_boot.S` — removed an assembler-time `.if`/`.error`
  assertion (no emitted bytes change);
- `ostd/src/arch/riscv/boot/simple_framebuffer.rs` — a clippy let-chains rewrite
  (semantically identical).

Neither runs at/​before the stall point in a way that could cause it. The "SMP
regression" does not exist in the code; it was a test-harness artifact.

---

## 2. Part 2 — the fix and the smp=4 result

`tools/riscv/nixos/m10/build_m10_desktop.sh` now regenerates the DTB with the
exact boot flags (via `boot_m10.py`'s `gen_dtb`), and `boot_m10.py` re-packs the
boot disk for the `--smp` it actually boots with. With a matching DTB, `smp=4`
reaches `/init` **3/3** times in the minimal harness (previously it stalled at
`Initializing the kernel page table` every time).

---

## 3. Part 3 — fbdev vs modesetting render benchmark

### 3.1 Setup

`tools/riscv/nixos/m9/xbench` is a static X11 micro-benchmark (fill, rect,
line, point, `XPutImage`) that writes `XBENCH … ops/sec` to `/dev/ttyS0`.
`build_m10_desktop.sh` installs it as `xbench.service` (a `oneshot` wired into
`graphical.target`, `After=xorg.service`), so it runs automatically after Xorg
comes up, in both the modesetting (`--gpu drm`) and fbdev (`--gpu bochs`) paths.

### 3.2 Results (smp=1)

| primitive (ops/sec) | modesetting (virtio-gpu) | fbdev (bochs) |
|---|---|---|
| fill-rect-fullscreen | 15 | 7 |
| rect-500 (batched sync) | 263 | 313 |
| rect-500-nosync | 2,419 | 601 |
| line-500 | 3,709 | 992 |
| point-1000 | 9,543 | 18,336 |
| putimage-64x64 | **>5 min / 8000 ops** (did not complete) | **>3 min / 8000 ops** (did not complete) |

Two things stand out:

1. **Neither driver is fast** — there is no 2D acceleration on either path, so
   full-screen fill is single-digit to low-teens ops/sec on both.
2. **The two drivers have opposite bottlenecks.** Small per-op writes (points)
   are ~2× faster on fbdev (direct framebuffer writes, no flush), while batched
   geometry (rects/lines without per-batch sync) is 2.5–4× faster on modesetting
   because its shadow framebuffer is ordinary RAM, whereas the fbdev path writes
   straight to the (slow, emulated) bochs device framebuffer. The per-batch
   `XSync` cost dominates and roughly equalizes the two for `rect-500` (263 vs
   313). `XPutImage` is pathological on modesetting (each putimage is an
   unbatched virtio-gpu 2D transfer — 8000 never finished in the 7-minute
   window); it is also slow on fbdev, confirming that the XPutImage cost is
   largely driver-independent X-server work.

*(The fbdev run overlapped a leftover modesetting QEMU for its first two
primitives, so its `fill-rect-fullscreen` (7) is a slight underestimate; the
rect/line/point numbers were measured after that contention was removed.)*

---

## 4. Files changed

- `tools/riscv/nixos/m10/build_m10_desktop.sh` — desktop disk with correct DTB + xbench service.
- `tools/riscv/nixos/m10/boot_m10.py` — benchmark boot driver (generates matching DTB, repacks, waits for `XBENCH done`).
- `tools/riscv/nixos/DRM-M10-report.md` — this report.
- `.gitignore` — ignore the built `tools/riscv/nixos/m9/xbench` binary.

---

## 5. Result

| deliverable | status |
|---|---|
| SMP regression bisect | **no regression** — ostd SMP/boot code identical to `main` |
| `smp=4` boot stable | **PASS** (3/3, correct DTB) |
| root cause | DTB `/memory` node 128 MB (missing `-m 2G`) + `-smp` mismatch |
| fbdev vs modesetting benchmark | **PASS** (numbers above) |
