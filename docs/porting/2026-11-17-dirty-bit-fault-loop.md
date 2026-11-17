# The EIC7700 faults on a store to a writable, not-yet-dirty entry (2026-11-17)

Status: **root cause found and fixed**, verified on the board with a
deterministic test and with cold desktop boots. Nothing is pushed.

This is the defect behind the "desktop boot race" of
`2026-11-17-desktop-boot-race.md`: a user store can fault forever at one
address while the handler reports success every time.

## The mechanism

A read fault on a writable mapping maps the page writable and marks it
accessed, but not dirty (`handle_single_page_fault`):

```rust
let mut page_flags = vm_perms.into();   // R | W for a writable mapping
page_flags |= PageFlags::ACCESSED;
if is_write {
    page_flags |= PageFlags::DIRTY;     // not taken: this fault is a load
}
cursor.map(frame, map_prop);
```

The EIC7700 raises a page fault for a **store to a present, writable entry
whose DIRTY bit is clear**. That fault reaches the "already handled" branch of
the same function, whose whole response was a TLB flush:

```rust
if VmPerms::from(prop.flags).contains(required_perms) {
    TlbFlushOp::for_range(va).perform_on_current();
    return Ok(());          // the entry still has D=0, so the store faults again
}
```

Nothing in that branch changes the entry, so the retried store faults again,
forever -- the same address, `handled=true` every time, no further syscall.
It is not a cache-coherence problem, so a flush can never resolve it.

Why it looked like a timing race: it only happens when the *first* access to a
page is a load and a later access to the same page is a store. When the first
access is a store, the single-page path sets D and the page never faults again.
QEMU never reproduces it because its MMU sets A/D itself.

## Deterministic reproduction

`/tmp/kx/adt2.c` (an initramfs init, raw syscalls) does two things:

1. control: `mmap` a fresh page, **store** first -- passes on every kernel;
2. the case under test: `mmap` a fresh page, **load** first, then **store to
   the same word**.

Board results, same command line, same initramfs:

| Kernel | control | load-then-store |
| --- | --- | --- |
| rebase, before this fix (sha `80e12006`) | survives | prints `about to store the same page`, then hangs; hardware watchdog resets the board |
| rebase, with this fix (sha `7445250f`) | survives | `store survived`, `second store survived`, `all done` |

So the hang is 100 % reproducible with this access pattern and the fix removes
it; the desktop-coincidence is what made it look flaky.

## The fix

`kernel/core/src/vm/vmar/vm_mapping.rs`, in the branch whose entry already
grants the faulting access: record the access on the entry before returning.

```rust
let needed = PageFlags::ACCESSED
    | if is_write { PageFlags::DIRTY } else { PageFlags::empty() };
if !prop.flags.contains(needed) {
    cursor.protect_next(PAGE_SIZE, move |flags, _cache| *flags |= needed);
    cursor.flusher().issue_tlb_flush(TlbFlushOp::for_range(page_aligned_addr..));
    cursor.flusher().dispatch_tlb_flush();
}
TlbFlushOp::for_range(page_aligned_addr..).perform_on_current();
return Ok(());
```

The branch keeps its other purpose -- another thread may have resolved the
fault concurrently, in which case the bits are already there and only the flush
runs. Recording A/D is what the access itself would have done, so it cannot
grant more than the entry already had.

This is the same rule as `06ea8220e` (protect must keep ACCESSED/DIRTY, COW
must flush locally), applied to the one path that previously observed the bits
and did nothing about them. The generic statement is: on this board the kernel
must maintain A and D itself on every path that touches a user entry.

## Second defect found while tracing: the windowed prefetch read the wrong pages

`effa4df88`'s `prefetch_run` was called with page numbers relative to the
*mapping*, while `Vmo::prefetch_run` indexes the *VMO*; every caller that
passes mapping-relative offsets through `MappedVmo::try_operate_on_range` gets
`self.offset` added, and the prefetch call did not. File mappings whose VMO
offset is non-zero (every ELF segment with `p_offset != 0`, every `mmap` with a
file offset) therefore read a window of the wrong file pages -- wasted I/O, and
`EINVAL` past EOF.

The same function also decided what to read without holding the page lock and
without re-checking; a page another thread was already reading could be
submitted twice. Both are fixed in `kernel/core/src/vm/page_cache/vmo/mod.rs`:
the caller adds `vmo.offset()`, and the run is built by locking each page and
re-checking its state first.

`windowed_read_submits_one_batch_per_run` (the batch-count test) passes with
the change: 1 passed, 0 failed, 252 filtered out in the `aster-core` ktest run.

## The rootfs corruption that hid this

The board's p2 was full (a test kernel too many) and then ext2-corrupt:
`free_blocks: refusing to free already-free block`, Firefox `Input/output
error`, and U-Boot could not load files that the running system had written.
A single `e2fsck -fy` from a repair initramfs was not enough -- a read-only
`-fn` pass still reported a block-bitmap difference -- a **second** `-fy` pass
fixed the rest and `-fn` then returned 0. `/var/log/journal` alone held 144 MB;
vacuuming it and deleting stale transfers brought p2 from 96 % back to 91 %
used.

With the filesystem clean, the *unfixed* kernel booted the desktop in 4/4 cold
boots, which is why the fault storm then looked unreproducible; the defect is
still there and the deterministic test above is what proves it.

## Verification

* `adt2` load-then-store: hangs before the fix, completes after it (table
  above).
* Cold desktop boots on the fixed kernel (sha `0ab45aa0`, no trace probes):
  `DESKTOP_READY` in each run, classified by `/tmp/board-net/boot_once.py`.
* `aster-core` ktest `windowed_read_submits_one_batch_per_run`: 1 passed.
* Full desktop through the deployed path: the fixed kernel was copied over
  `/root/kernel-rel.img` (the original kept as `kernel-rel.img.orig`, then
  removed), `run bootcmd` in U-Boot booted it to
  `ASTERINAS_DESKTOP_BOOT_READY` in 13--14 s with
  `ASTERINAS_USERSPACE_REBOOT_DISARMED reason=desktop-ready`; Xorg opened both
  evdev devices (`2` `Using input driver "evdev"` entries,
  `/dev/input/event0..1`), `/dev/fb0` present, `xwd` grabbed an 8 MB screen,
  and `curl https://www.baidu.com` returned 200 with the page body. eBPF:
  verifier refusals, `load` (`map_fd=3 prog_fd=4`), `count`/`percpu`/`ringbuf`
  sampling, `sockfilter` PASS, `ksym __kernel_start -> 0xffffffff80200000`.
* The three board fixes from `06ea8220e` are unchanged and still in the tree.

## Board operations learned the hard way (2026-11-17, evening)

* **Use `run bootcmd` (boot.scr) for boots; the typed `booti` harness is
  currently unreliable on this board.** From ~13:30 the manual path
  (`boot_once.py`, identical addresses, args, kernel bytes, initrd and dtb)
  hung every kernel -- the fixed one, the unfixed probe build, *and* the
  deployed release image -- at a different early point each time (component
  init, stage1, or the first desktop services), while `boot.scr` booted the
  same images cleanly one minute later. The kernel image read back from p2
  matched the host CRC, the loads reported the same sizes, and the bootargs
  were byte-identical to boot.scr's. Not explained; recorded so the next
  session starts from boot.scr.
* **The repair initramfs lives on the data partition**, `mmc 1:3
  repair.cpio.gz`; `mmc 1:1` has no copy, and boot.scr's fallback tries p1
  first. Booting the repair image by hand therefore needs `mmc 1:3`.
* **Two `e2fsck -fy` passes are the minimum** after a full-disk episode; the
  second run of the day (after the first repair) reported `FSCK_RC=0` and its
  read-only pass `VERIFY_RC=0`.
* **A file copied by the running system onto a nearly-full p2 can land on slow
  blocks.** `/root/kernel-rel.img` written with `cp` read back at 4.1 s for
  9 MB (0.7 s is normal) and pushed the boot past the watchdog window, which
  turned into a restart loop until the file was rewritten with U-Boot's
  `ext4write` from a good copy (9.1 MiB/s again, CRC equal). Check the load
  time, not only `bytes read`.
* `/var/log/journal` grows by tens of MB per boot; vacuum it before boot tests
  on this 3 GB partition.

## Artifacts

`/tmp/kx/adt2.c` + `/tmp/kx/adt2.cpio.gz` (probe), `/tmp/board-net/boot_adt.py`
(boots it with the on-board kernel and the watchdog armed),
`/tmp/board-net/recover_uboot.py` (interrupts autoboot to reach `=>`),
`/tmp/runs/fix-*.log` (desktop boots of the fixed kernel).
