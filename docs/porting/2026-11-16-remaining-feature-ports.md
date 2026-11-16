# Remaining downstream feature ports (2026-11-16)

Status of the four tracked items and, for each unfinished one, the tree facts
that decide how the port has to be shaped. Written after the rebase was
verified on the board; the intent is that the next session does not have to
re-derive the same conclusions.

## Done and board-verified

* `/sys/class/net` — `f9a67e83c`. On Megrez: `address=00:48:54:71:00:47`,
  `mtu=1514`, `flags=0x11043`, `operstate=up`.
* `SOCK_DIAG` — `36588ff22`, `d560aee8c`, `95a47d885`, `ae3c8460c`,
  `ffe48a4f1`, `beb7228b8`. On Megrez a raw netlink client gets
  `SOCK_DIAG_BY_FAMILY` with `sport=12345`, `inode=5`, then `NLMSG_DONE`.
  TCP sockets are not registered yet, so a TCP dump only returns DONE, and the
  uid column is zero.

## 1. cgroup events inode cache

The rebase serves `cgroup.events` as a systree attribute computed in
`read_attr_at` (`kernel/core/src/fs/fs_impls/cgroupfs/systree_node.rs`); there
is no per-node events inode, `cgroupfs/inode.rs` has no `EventsInodeCache`, and
`propagate_{add,sub}_populated` return nothing. The downstream fix
(`7e9dcc0cb`) assumed a real inode per node and a propagation that reports the
nodes whose populated state flipped.

Shape to implement here:

1. Keep the attribute, but give `CgroupNode` an `events_inode: Weak<...>` slot
   filled when the events file inode is created, so membership changes have
   something to wake.
2. After `CgroupMembership::{move,add,remove}_process` mutate a node, publish
   `FsEvents::MODIFY` through `inode.fs_event_publisher()` when the node's
   populated state changed (and for ancestors whose populated count flipped).
3. Test: a reader that polls `cgroup.events` wakes on migration; no publisher
   exists when nothing is subscribed, so a shutdown cannot be delayed by
   notifications nobody asked for.

## 2. page-cache cold-page batch read

`BackedVmo::commit_range` does not exist in this tree: the downstream windowed
read (`d5024af32`) was written against `read_page_async(idx, LockedCachePage,
io_batch)`, while this tree has upstream's `read_pages_async(PageRun<'_>,
io_batch)` and a TODO in `vm/page_cache/tests/mod.rs` recording the gap.

Shape to implement here: a window helper that collects up to 16 cold pages of
one VMO into a `PageRun` and calls `read_pages_async` once; ext2 then coalesces
the contiguous runs into one BIO inside its own `read_pages_async`. The existing
test utilities already count backend batch calls (`read_batch_calls`), so the
test is "one call per window, N pages per call".

## 3. Megrez dual xHCI

Downstream `6d9a52b59` brings up the controller and reports port hotplug, and
touches `audio-hda`. The rebased tree registers one controller; the board's
second controller is what the downstream commit enumerates. Port it on top of
the current device model (`kernel/core/comps/device`) and verify with the
board's two root ports.

### Done (2026-11-16): `092162b00`

The NUL-separated `/chosen/asterinas,usb-host` list was already understood; the
gap was that the driver kept exactly one host in process-wide statics
(`HOST_RESOURCES`, `KEYBOARD`, `KEYBOARD_WAKER`, `IRQ_LINE`, and a deferred
keyboard decoder). Now:

* `init` collects every controller that validates into `HOST_CONFIGS`;
  `host_count`/`run_host(index)` let the kernel spawn one worker per controller
  (`kernel/core/src/init.rs`).
* A worker owns its MMIO mapping, IRQ mapping, waker, decoders and input
  devices. Mouse reports, previously dropped by the loop, are decoded through
  the descriptor-derived layout and registered as their own input device.

Two MMIO rules decide how the mapping reaches a worker, and both had already
broken the previous board boot:

* `PollingUsbHidHost::open` rejects a mapping whose `IoMem` is shared
  (`is_unique` = the `KVirtArea` has one owner), so cloning the stored `IoMem`
  fails with `InvalidMmio`. The PCI host config is taken
  (`take_host_config`), and DWC3 workers acquire their range directly.
* `IoMemAllocator::recycle` is dead code: an MMIO range is never freed when its
  `IoMem` drops. A range probed in `init` could not be acquired again by the
  worker, so the capability probe moved into the worker.

Board result: both workers start (`Starting USB host 0/1`), each controller
reports its single connected port, and the loops split the devices —
`keyboard=true, mouse=false` on one and `keyboard=false, mouse=true` on the
other. The single-host driver could only ever serve one of the two sockets.
QEMU virt still takes the PCI path; a controller with no HID device now logs
that instead of an error.

## Testing recipe used for the finished items

Initramfs test programs are raw-syscall statics (`-nostdlib -static -Os
-fno-builtin`) that write to `/dev/ttyS0` with a fallback to fd 1, because the
board's init stdout is the VT. Procfs and sysfs must be mounted by the init
before either is read — an unmounted `/proc` reads as a missing file and cost
one debugging cycle already.

### Booting a direct-init test kernel on the board

`megrez_console.py boot` only validates stage1 (`--root-init=`) command lines,
so a `init=/init` handoff is driven directly through the bridge FIFO
(`/tmp/board-net/boot_cg.py` does this):

1. Confirm the persistent bootcmd still boots the deployed system — on this
   board it is `ext4load ... /root/boot.scr && source`, not a literal `booti`.
2. Interrupt autoboot by writing a line containing one space to the FIFO (a
   bare space is never flushed: the bridge flushes on LF only).
3. `ext4load mmc 1:2` the initrd and kernel, `ext4load mmc 1:1` the DTB, set
   `bootargs`, compare the reported kernel size with the local file.
4. Arm the DesignWare watchdog exactly as bootcmd does, then
   `booti 0x80200000 0x88300000:${initrd_size} 0x88000000` with
   `asterinas.reboot_after=120 asterinas.panic_reboot=1
   asterinas.hardware_watchdog_handoff=1` and both `mmc_write_partition2/3`.
5. The deadline returns the board to U-Boot and then to the deployed desktop
   on its own; nothing has to be recovered by hand.

Prefer the network for the transfer (`scp` to `/root` while the deployed
desktop is portal-authenticated): the dev kernel image is ~20 MiB, far past the
console-push budget.

## Update: what the cgroup.events poll path needs (2026-11-16, later)

Reading the code settled three questions:

* `poll` is not a `FileOps` method. `FileOps` only has `read_at`/`write_at`/
  `readdir_at`; polling lives on the open file (`InodeHandle::poll`), whose
  fallback branch returns `IN | OUT & mask`, so a regular file is always
  "readable" and `POLLPRI` is never ready. That is why the QEMU test saw
  `POLLIN` fire immediately and `POLLPRI` never fire.
* `FsEventPublisher` takes subscribers through `add_subscriber(Arc<dyn
  FsEventSubscriber>)` and broadcasts with `publish_event`. A subscriber
  implements `deliver_event(&self, FsEvents, Option<String>) -> bool`,
  `interesting_events() -> FsEvents` and optionally `is_oneshot_and_dead()`.
  It must not sleep: the publisher may hold a spin lock.
* `notify_events_modified()` currently publishes on the cgroup *directory*
  inode, so even a working poll path on `cgroup.events` would not see it; the
  weak reference has to be filled from that file's inode instead.

So the work is: a waker subscriber in `InodeHandle` that sets `IoEvents::PRI`,
cleared when the file is read; the events file's inode stored on the node; and
the test in `/tmp/board-net/cgtest.c` (already switched to `ppoll` with
`POLLPRI`, since riscv64 has no `poll` syscall — 7 is `fsetxattr` there).

## Update: cgroup.events polling works (2026-11-16, final)

The wake path is in and verified in QEMU (`/tmp/board-net/cgtest2.c`). Three
things had to change beyond the plan above, each invisible until the previous
one was fixed:

* **`fs_event_publisher()` does not create the publisher.** It returns `None`
  until someone calls `fs_event_publisher_or_init()`, so a reader that polls
  before any event exists subscribed to nothing, and the notifier (which used
  the non-initializing getter, so an unsubscribed cgroup still costs nothing)
  found nothing to publish to. `InodeHandle` now initializes on first poll.
* **`Pollee` caches the events its check closure produced.** `notify()`
  invalidates that cache, but a subscription can cache `PRI` *after* the
  notify, and a read that only cleared the pending flag left the cached `PRI`
  visible. `InodeHandle::read` invalidates the pollee as well, which is what
  makes the event level-triggered.
* **`cgroup.events` inodes are per lookup.** Every `lookup` on the attribute
  builds a fresh inode, so a single weak slot is silently overwritten by any
  intervening `stat` or second opener. `CgroupNode` now keeps a `Vec` of weak
  inodes, notifies all live ones and prunes dead entries.

The node also wakes **itself**, not only its ancestors: moving the first
process into a cgroup flips that cgroup's own `populated`, which the earlier
`propagate_*`-only notification missed.

Verified sequence: `poll` times out before migration; after writing the pid to
`cgroup.procs` the reader wakes with `POLLPRI` even though the file was looked
up a second time in between; `read` returns `populated 1` / `frozen 0` and
clears `POLLPRI`; moving the process back to the root cgroup wakes it again.

Known gap: the hierarchy root (`/sys/cgroup.events`, served by `CgroupSystem`,
not `CgroupNode`) still has no notifier. Nothing in a systemd-style workload
polls it, and the root's `populated` is not tracked there.

