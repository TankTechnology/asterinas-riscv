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

## Testing recipe used for the finished items

Initramfs test programs are raw-syscall statics (`-nostdlib -static -Os
-fno-builtin`) that write to `/dev/ttyS0` with a fallback to fd 1, because the
board's init stdout is the VT. Procfs and sysfs must be mounted by the init
before either is read — an unmounted `/proc` reads as a missing file and cost
one debugging cycle already.
