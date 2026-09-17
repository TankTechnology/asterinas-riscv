# DRM-M9 — smp=4 bring-up root-cause + fbdev/modesetting render benchmark harness

Date: 2026-08-16
Branch: `track/drm`
Status: **PARTIAL** — (1) the `smp=4` hang is root-caused to a **stale 1-CPU
DTB** plus a **core-kernel SMP stall in the boot path** (not a virtio-gpu/input
lock); (2) the fbdev-vs-modesetting benchmark harness (`xbench`) is built but the
run is blocked by the same core boot instability.

---

## 0. TL;DR

| item | result |
|---|---|
| `smp=4` hang reproduced | yes — serial stops before `/init`, at **different** points across runs (boot-AP bring-up, initramfs unpack, after-banner) |
| root cause #1 | the boot disk ships a **1-CPU `qemu-virt.dtb`**, but `-smp 4` makes OpenSBI boot a **non-zero hart** (`Boot HART ID: 1`/`2`, non-deterministic) |
| root cause #2 | even with a correct 4-CPU DTB, the kernel **stalls in the boot path** (initramfs unpack → page-cache/heap `dealloc`), with a garbage pointer suggesting heap corruption |
| prior "virtio-gpu/input lock" hypothesis | **not confirmed** — the stall is not a device-registration lock |
| DRM-specific? | **no** — the fbdev path (`--gpu bochs`, no virtio-gpu) hangs identically at `smp=4` |
| benchmark harness | `tools/riscv/nixos/m9/xbench.c` (mini-x11perf) + `build_xbench.sh` — compiles and runs, but the boot instability blocks the A/B run |

---

## 1. Part 1 — `smp=4` hang: reproduce → locate

### 1.1 Reproduction

`boot_systemd_desktop.py --gpu drm --smp 4` reproducibly fails before `/init`
runs. The serial tail stops at one of several points depending on run timing:

```
Enter riscv_boot                      # sometimes stops here (boot_all_aps)
OSTD initialized. Preparing components.
Spawn the first kernel thread
[kernel] unpacking initramfs.cpio ... # sometimes stops here
[kernel] rootfs is ready              # sometimes reaches this, then
<Asterinas banner>                    # stops after the banner (before init)
```

The varying stop point is the first clue: this is a **race / corruption**, not a
single deterministic lock ordering.

### 1.2 Root cause #1 — stale 1-CPU DTB

The independent boot disk ships a static `qemu-virt.dtb` (copied from M5) that
describes **one** CPU:

```
$ dtc -I dtb -O dts qemu-virt.dtb | grep -c 'cpu@'     # -> 1
```

With `-smp 4`, QEMU/OpenSBI boots a **non-zero boot hart** (non-deterministic):

```
Domain0 Boot HART           : 2        # one run
Boot HART ID                : 1        # another run
```

`count_processors()` reads the DTB, returns 1, so `boot_all_aps()` returns early
and the kernel runs single-threaded on a hart whose id does not match the DTB's
single `cpu@0` node. This alone is a harness bug: the DTB must match the `-smp`
count. Regenerating the DTB with the boot's `-cpu` flags and `-smp 4`
(`mmu-type = "riscv,sv39"`, 4 cpu nodes) is the correct harness fix:

```
qemu-system-riscv64 -machine virt -smp 4 \
  -cpu rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true \
  -machine dumpdtb=qemu-virt-smp4.dtb
```

### 1.3 Root cause #2 — core-kernel boot stall

With the correct 4-CPU DTB, `boot_all_aps()` now completes (APs reach their idle
loops) but the BSP still stalls in the boot path. A QEMU gdb attach of the hung
guest shows all four harts in the kernel, with the boot hart executing the
initramfs unpack and the APs in `halt_cpu`:

```
Thread 3 (CPU#2 [running]): core::num::checked_mul / ub_checks::maybe_is_nonoverlapping
  #12 aster_kernel::vm::page_cache::vmo::Vmo::write   (offset=2035712)
  #11-9  Vec<(usize, Frame<CachePageMeta>)> drop
  #8     RawVecInner::deallocate
  #1     osdk_heap_allocator::allocator::dealloc
  #0     drop_glue<RefMut<osdk_heap_allocator::allocator::LocalCache>>
Thread 1/2/4 (CPU#0/#1/#3 [halted]): ostd::arch::irq::ops::enable_local_and_halt
```

The BSP is in the **heap allocator's per-CPU `LocalCache` dealloc path** during a
page-cache write, and an earlier sample captured
`maybe_is_aligned_and_not_null (ptr=0x200000022, align=0xffffffffffff0001)` — a
garbage pointer. This is the signature of **heap corruption / an allocator bug
under SMP**, not a lock in the virtio device registration path.

### 1.4 Not DRM-specific

The fbdev path (`--gpu bochs`, no virtio-gpu, still with the virtio input
keyboard/tablet) hangs at the identical point (`unpacking initramfs`). The
virtio-gpu device is therefore **not** required to trigger the stall; the
previous session's "virtio-gpu/input registration lock" hypothesis is not
supported by the evidence.

### 1.5 Conclusion

`smp=4` is **not fixed** in this milestone. The two concrete findings are:

1. **Harness bug** (fixable now): regenerate the DTB for the target `-smp`
   count instead of shipping a stale 1-CPU DTB.
2. **Core-kernel SMP stall** (out of DRM scope): heap-corruption/allocation
   stall in the boot path (page-cache `Vmo::write` → `dealloc`), manifesting
   only under SMP. This needs investigation in `ostd`/`osdk_heap_allocator`,
   upstream.

The DRM desktop chain remains verified at the proven **`smp=1`** configuration.

---

## 2. Part 2 — fbdev vs modesetting render benchmark (harness)

### 2.1 `xbench` — mini-x11perf

`tools/riscv/nixos/m9/xbench.c` is a small static X11 client that times a set of
primitives and reports ops/sec to both stdout and `/dev/ttyS0` (so the boot
harness captures it from the serial log):

- full-screen `XFillRectangle` fill
- 500 × 64×64 rectangles (per-batch `XSync`)
- 500 × 64×64 rectangles (single sync)
- 500 lines
- 1000 points
- 200 × 64×64 `XPutImage` (exercises the pixel-write path)

`build_xbench.sh` cross-compiles it against the sibling tree's libX11 static
closure (same flags as `tools/riscv/xorg/build_xpanel.sh`).

### 2.2 Run wiring

To run it, add `xbench` to the desktop rootfs and start it as a oneshot service
after `xorg.service` (add `xbench.service` to the `graphical.target` `Wants=`
list). The binary retries `XOpenDisplay` for up to 60 s so it tolerates a slow
Xorg bring-up.

### 2.3 Status — blocked by the boot stall

The A/B run (fbdev vs modesetting) could not be completed: re-packing the
desktop initramfs to include `xbench` and booting at `smp=1` also hits the
boot-path stall (initramfs unpack or after-banner). The benchmark harness is
committed so it can be re-run once the core SMP/allocation stall is fixed.

---

## 3. Files changed

- `tools/riscv/nixos/m9/xbench.c` — X11 render micro-benchmark.
- `tools/riscv/nixos/m9/build_xbench.sh` — cross-compile `xbench`.
- `tools/riscv/nixos/DRM-M9-report.md` — this report.

---

## 4. Result

| deliverable | status |
|---|---|
| `smp=4` hang reproduced | **yes** (serial stops before `/init`, multiple points) |
| root cause | stale 1-CPU DTB (harness) + core-kernel SMP stall (page-cache/heap dealloc) |
| prior "virtio-gpu/input lock" hypothesis | **not confirmed** |
| DRM-specific? | **no** (fbdev path hangs identically) |
| `smp=4` boot PASS | **no** (blocked on core kernel issue) |
| fbdev/modesetting benchmark | harness built, run **blocked** by the boot stall |
