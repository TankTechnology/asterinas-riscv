# Booting a Debian root filesystem on the x86_64 Asterinas kernel

Before this change the x86_64 build could only boot an initramfs. There was no
Debian userspace on it, and the x86 CI job runs `bench_linux_and_aster.sh`
with a fixed job list — it has never used `make results`. This is a new
capability on x86 rather than a gap being closed.

The RISC-V workflow was already there, and most of it transfers: the rootfs
builder, its package contract, and the Stage1 root handoff are architecture
neutral. What did not transfer was the boot.

## The boot path

The RISC-V guest is started with `-kernel`/`-initrd`/`-append` under U-Boot.
x86 has no equivalent pre-UEFI handoff, but it does not need one: QEMU loads a
multiboot2 ELF itself. `-kernel` takes the Asterinas ELF — OSDK patches its
`e_machine` to `EM_386` for exactly this, because QEMU refuses an `EM_X86_64`
multiboot image — and `-initrd` becomes the first multiboot2 module, which the
kernel magic-sniffs as a plain cpio rather than gzip.

That means the Stage1 initramfs works unmodified. It finds the root disk by
reading the ext2/ext4 volume label out of each candidate's superblock, so the
Debian image may sit on any virtio-blk slot; here it lands on `/dev/vdd`.

```
DEBIAN_STAGE1_PROGRESS step=start mode=systemd
DEBIAN_STAGE1_PROGRESS step=probe-block device=/dev/vda … /dev/vdb … /dev/vdc
DEBIAN_STAGE1_PROGRESS step=probe-complete result=match
DEBIAN_STAGE1_PROGRESS step=root-found device=/dev/vdd
DEBIAN_STAGE1_PROGRESS step=handoff-enter action=root-mount … dev-bind …
    api-directories … run-mount … tmp-mount … chroot … chdir … exec
systemd 257.13-1~deb13u1 running in system mode
Detected architecture x86-64.
Welcome to Debian GNU/Linux 13 (trixie)!
[  OK  ] Reached target multi-user.target - Multi-User System.
[  OK  ] Reached target graphical.target - Graphical Interface.
```

### Why not the OSDK scheme

`cargo osdk run` is the project's normal path, and it cannot express this boot:

- `[run.boot]` in `OSDK.toml` always appends `sh -l` to the init arguments,
  which the standard test initramfs consumes as a command to run. There is no
  CLI flag or scheme that removes them — `--init-args` appends, and a named
  scheme inherits the global arguments ahead of its own. Stage1's
  `parse_root_init` rejects any token it does not recognise, so a command line
  built by OSDK can never reach it.
- The `normal` scheme attaches three auxiliary conformance images, so it
  cannot boot a Debian disk on its own.

`tools/x86/debian/boot_debian.sh` therefore passes the QEMU command line
directly. It advertises `--print-argv` and `--print-append` so the command line
is asserted in tests without running QEMU: one drive, no `-bios`, no `-cdrom`,
and `init=/init -- --root-init=systemd --root-fs=ext4` in the append string.

## What had to change

**The Stage1 systemd handoff failed on a minimal initramfs.** `/newroot/run` is
a fresh tmpfs, and when the root label is not the M2 one the handoff binds the
initramfs's `/usr/lib/asterinas` into `/newroot/run/asterinas-tools` to lend
the root the desktop helper scripts. An initramfs built with `--minimal` has no
such directory, and binding an absent source fails, which ended the whole
handoff at `handoff-failed action=run-mount`. The bind is now skipped when the
source is absent; the helpers are an accessory, not a requirement.

**`systemd-journald.service` timed out.** It failed with `result 'timeout'`,
and because the journal socket is ordered before most of the boot, the start
job sat there while every dependent unit waited. The kernel cannot complete
systemd's `Type=notify` readiness handshake. The scratch disk now carries
`/etc/systemd/system/systemd-journald.service.d/asterinas-notify-compat.conf`
with `Type=simple` and `NotifyAccess=none`. This is a guest-side workaround, the
same shape the desktop work already needed for logind, and it is not a kernel
change.

**Log level.** At the default level the kernel prints a line per syscall and
`ldconfig.service` alone runs past 100 seconds of guest time; the boot only
proceeds at `loglevel=4`. The boot script defaults to that and says why.

### Architecture parameterization

The modules that freeze a rootfs identity assumed RISC-V in more places than
the surface suggested:

| Module | What assumed RISC-V |
| --- | --- |
| `contract.py` | The manifest architecture, the gate-package architecture filter, and the writer's own re-validation through the verifier's code path |
| `signed_sources.py` | The `Architectures:` field of `InRelease` and the apt `Packages` index paths |
| `build_rootfs.sh` | Installed the user-mode emulator unconditionally, so a native build ran `install` with an empty name |
| `build_stage1.sh` | The compiler and the default output directory; the probe's syscall numbers |
| `lmbench_native.py` | The GNU triple and the Nix `target` argument |

`stage1_probe.c` carried the RISC-V generic syscall numbers as constants and
now takes them from `<sys/syscall.h>`. On RISC-V the numbers are unchanged; the
probe *names* stay `syscall213`/`syscall272` because they are labels the megrez
probe, its validators and its recorded transcripts share.

## Non-fatal gaps observed

Each of these is reported by systemd and then worked around or ignored; none
stops the boot:

- `Failed to open netlink, ignoring: Protocol not available`
- `Failed to bump fs.file-max` / `fs.nr_open`: the sysctls do not exist
- `bpf-restrict-fs: BPF LSM hook not enabled in the kernel`
- `systemd-sysctl.service` fails: no `/proc/sys` tree to apply
- `dev-hvc0.device` times out after 90 s; `serial-getty@hvc0.service` then fails
  its dependency and no login prompt is offered on the console
- `Failed to find module 'autofs4'` / `'unix'`: no module loading

## The image had to be repaired before it could be trusted

`mke2fs -d` populates the tree with e2fsprogs' own copier rather than a mounted
filesystem, and it does not always get the block and inode bitmaps right. The
x86 rootfs shipped with blocks 298798-298799 marked free while inode 179
(`/etc/machine-id`) and inode 19447 already owned them, plus an unattached
zero-length inode. `Last mount time: n/a` and `Mount count: 0` on that image
show it was never mounted, so the damage is produced at build time.

That is not cosmetic. Every later write trusts the bitmap, so it allocates a
block already in use; two inodes then own it and the next writer destroys the
other one's data. Here the Nix dynamic loader landed on a clobbered block, so
every Nix-linked binary died with SIGSEGV at startup while Debian-linked ones
were untouched — which reads exactly like a kernel fault and is not one. The
guest reads the file precisely as stored; the bytes on disk were already wrong.

`build_rootfs.sh` now runs `e2fsck -fy` once the image is populated and then
requires a clean `e2fsck -fn`, so an inconsistent image cannot be published.

## A boot that never syncs leaves the image inconsistent

That repair only covers the image as it is built. Booting it makes the image
inconsistent again, and the reason is a scheduling gap inside the ext2 driver
rather than anything the guest does wrong.

The per-group inode table is an ordinary `PageCache`
(`inode_table_cache: PageCache` in `ext2/block_group.rs`), so the kernel's
normal dirty-page writeback flushes it. The allocation bitmaps are not: they
are plain in-memory `Dirty<Bitmap>` values, and the superblock counters
likewise. Those reach the disk only through `BlockGroup::sync_metadata`, which
is reached only from `Ext2::sync_all` (`ext2/fs.rs:793`), which is reached only
from `FileSystem::sync` (`impl_for_vfs/fs.rs:28`) — that is, from `sync(2)`,
`syncfs(2)` or unmount, and from nowhere else. `Ext2::create_inode`
(`ext2/fs.rs:682`) writes a placeholder descriptor — link count set, size 0, no
blocks, `dtime` 0 — into the inode table as soon as it allocates the number,
before `make_empty` or the parent's `add_entry` has run. A created inode's
descriptor can therefore reach the disk while the allocation that authorises it
cannot.

Measured, by booting the published base image and killing the VM once
`multi-user.target` appeared, then comparing it block by block against the
pristine base:

```
29 of 1048576 4K blocks differ
free inodes  242700 -> 242700   (unchanged)
free blocks  721560 -> 721560   (unchanged)
e2fsck files  19444 -> 19444    (unchanged)
```

Every changed block is an inode-table block or a data block; not one bitmap
block, group descriptor or superblock differs. Group 2's inode-table block
65730, all zeroes in the base, now holds descriptors for inodes 19457-19462
with consecutive generations `1792069615`-`1792069621`, `dtime` 0, link counts
2 (directories) and 1 (files), size 0 and no block pointers — except 19462, a
9469-byte file whose extents point at blocks 898/899/900, which are three of
the other changed blocks. `e2fsck` reports exactly that state: `Unattached
inode 19459` and `Inode bitmap differences: +(19445--19462)`.

A clean shutdown heals most of it, because systemd calls `sync(2)` on the way
down: the `lmbench-poweroff.ext2` image drops to 26 complaints and its free
inode count is correctly 1772 lower. What survives is the second half of the
same gap. An inode that was created successfully and whose metadata was then
never staged keeps `create_inode`'s placeholder forever, so `/root/.ssh` — a
`d- /root/.ssh :0700` line in systemd's `/usr/lib/tmpfiles.d/provision.conf` —
is a directory entry on disk pointing at a directory of size 0 with no data
block, and `e2fsck` calls its link count wrong.

This is not fixed. Making it right means the bitmaps, group descriptors and
counters have to become durable on the same schedule as the inode table, either
by participating in writeback or by going through the journal; that is not a
small change, and no half-fix was landed. It costs this route nothing, because
the guest root filesystem was already treated as an untrusted channel —
`boot_debian.sh` reads the serial transcript and nothing else.

## `/proc/net/dev` did not exist

The live kernel tree had no `/proc/net` at all: `RootDirOps::STATIC_ENTRIES`
listed cmdline, cpuinfo, filesystems, loadavg, meminfo, mounts, self, stat, sys,
thread-self, uptime and version, and nothing else. `/proc/sys/net/ipv4` was
there, `/proc/net` was not. The dead tree — everything under `kernel/src`,
which has not been compiled since the `aster-core` split — had both, so this
was a fix that was written once and then lost in the move.

Two things were missing, not one. `aster-bigtcp` had an `IfaceStats` counter
set and the public `IfaceStatsSnapshot` for it, but `iface/mod.rs` never
declared `mod stats`, so it was dead code with no callers; and nothing outside
`crate::net` could enumerate interfaces, because `iter_all_ifaces` is
`pub(in crate::net)`. Porting the file alone would have produced a `/proc/net/dev`
that printed zeroes forever.

Now `/proc/net` is a symlink to `self/net` as on Linux, `/proc/<pid>/net/dev`
prints the two header lines and one row per interface, and the counters are
recorded where packets actually cross the interface — rx in `poll_ingress`
right after `device.receive()` returns, tx at all three `device.send` sites,
counted only when the device accepts the packet.

Measured in the guest, with a throwaway unit in place of the benchmark:

```
              rx bytes   rx pkts   tx bytes   tx pkts
  eth0  t0:          0         0        168         4
  eth0  t1:        136         1       1344        32
```

`t1` is after two `curl` attempts at the slirp gateway, which refuses the
connection and therefore answers — that answer is the received packet.

Two things this does not do. `lo` stays at zero even while loopback traffic
flows, because the kernel treats its own addresses as local and hands the data
straight to the receiving socket without traversing the device, so no packet
crosses the interface and there is nothing to count; Linux would count it.
And the guest's address is hardcoded in the kernel (`net/iface/init.rs`, the
`10.0.2.15/24` via `10.0.2.2` profile), not leased, which is why a plain boot
shows transmitted packets and no received ones.

## Route-table tools blocked instead of failing

Every call below was run in the guest under `timeout 20`, and each reports its
own exit code. This is what the kernel did before the fix:

```
ip route                    rc=124   no output at all
ip route get 10.0.2.2       rc=124
ip -4 route show            rc=124
ip neigh                    rc=124
/proc/net/route             No such file or directory
/proc/net/arp               No such file or directory
ip link                     rc=0     works
ip addr show                rc=0     works
ss -tln                     rc=0, but "Cannot open netlink socket: Address family not supported"
curl http://10.0.2.2/       rc=124
```

It was not a guest-wide freeze: `ip link` and `ip addr` answered in the same
session. The stall was specific to requests the kernel never answered.
`RtnlSegment` had only `NewLink`/`GetLink`/`NewAddr`/`GetAddr`, so
RTM_GETROUTE and RTM_GETNEIGH fell to its `_` arm and produced no reply that
iproute2's dump filter would accept.

RTM_GETROUTE is now ported into the live tree from
`kernel/src/net/socket/netlink/route/kernel/route.rs`, which had it all along —
the same pattern as `/proc/net/dev` and `IfaceStats`: written once, lost when
the kernel was split into `aster-core`. The message layer came with it
(`RouteSegment`, `RouteAttr`, the `RtnlSegment` variants); only the namespace
argument had to change, because this kernel has a single network namespace and
no `NetNamespace` to ask.

```
$ ip route
default via 10.0.2.2 dev eth0
10.0.2.0/24 dev eth0 proto kernel scope link src 10.0.2.15
$ ip route get 10.0.2.2
10.0.2.2 dev eth0 src 10.0.2.15
    cache
$ ip route get 127.0.0.1
local 127.0.0.1 dev lo proto unspec src 127.0.0.1
$ ip -4 route show table all
default via 10.0.2.2 dev eth0
10.0.2.0/24 dev eth0 proto kernel scope link src 10.0.2.15
local 127.0.0.0/8 dev lo table local proto kernel scope host src 127.0.0.1
local 127.0.0.1 dev lo table local proto kernel scope host src 127.0.0.1
broadcast 127.255.255.255 dev lo table local proto kernel scope link src 127.0.0.1
local 10.0.2.15 dev eth0 table local proto kernel scope host src 10.0.2.15
broadcast 10.0.2.255 dev eth0 table local proto kernel scope link src 10.0.2.15
```

The remaining unimplemented dumps no longer block either. The dead tree had
already worked out the shape: recognise the request type, skip its payload, and
answer with an empty result terminated by `NLMSG_DONE`, instead of producing an
error segment that iproute2's dump filter will not accept. The live tree now
does the same for RTM_GETNEIGH, RTM_GETRULE, RTM_GETQDISC, RTM_GETTCLASS,
RTM_GETACTION, RTM_GETTFILTER, RTM_GETNEXTHOP and RTM_GETCHAIN:

```
ip neigh        (empty)  rc=0     was rc=124
ip -6 neigh     (empty)  rc=0
ip rule         (empty)  rc=0     was unimplemented
ip -6 route     (empty)  rc=0
```

What is still absent is the data behind them: `/proc/net/{route,arp,tcp,udp}`
do not exist, and the neighbour and rule tables are not exposed. What is no
longer true is the far worse failure mode — a tool that asks a question this
kernel cannot answer no longer hangs until it is killed.

## The storage milestone: what was wrong and what changed

The goal is an image that `e2fsck -fn` accepts after a clean shutdown. Two
earlier attempts are recorded below because each one taught something the final
fix needed. Both failed for a reason that turned out to be upstream of them.

**P0: every sync was failing.** `Ext2::sync_all` was instrumented at warning
level (`--loglevel 5`), and every invocation on every boot printed the same
pair:

```
ext2: sync begin (last allocated inode 19478)
ext2: group 0 dirty before sync: block_bitmap=true inode_bitmap=true descriptor=true cached_inodes=585
ext2: sync failed in group 0: Error { errno: EINVAL, msg: Some("inode number out of valid range") }
```

The rejection came from `Ext2::write_back_inode_desc`, which refused every
inode number below `s_first_ino`. Reserved inodes live there, and the journal
inode (`ino 8`; the image has `First inode: 11`) is synced by every journal
publication. The first metadata write therefore failed, so **no bitmap or group
descriptor was ever written**, at any point, by any sync. The inode table and
data blocks still reached the disk through their own writeback paths, which is
exactly the inconsistent image the checker reported. The last successful sync
was: none.

The fix is to accept reserved inode numbers on write-back. `s_first_ino` still
constrains new allocations, which `alloc_ino` checks separately.

**Attempt 1: stage the descriptor as soon as the create succeeds.** The
reasoning was that `create_inode` writes a placeholder (size 0, no blocks) and
the real descriptor only reaches the inode table when something later syncs the
inode, so staging it at the end of `create_initialized_child` should close that
window. It made things worse:

```
Extended attribute in inode 19447 has a hash (0) which is invalid
Inode 19447, i_blocks is 8, should be 16
Directory inode 19463, block #0, offset 0: directory corrupted
```

The new directory had just been given a data block holding `.` and `..`, and
that block reaches the disk through the page cache on its own schedule.
Staging a descriptor that points at it first put a block pointer on disk ahead
of the block's contents. **Metadata must not outrun data.**

**Attempt 2: `sync_data()` instead.** It writes the pages, then the indirect
metadata, then the descriptor, in that order. That removed the corruption and
the wrong xattr hash. It was reverted anyway: it put a synchronous flush on
every create for no completion of the goal.

**The atomicity gap that remains after P0.** Fixing the sync failure makes the
shutdown path able to write metadata, but the metadata was still split across
four schedules, so a crash between them leaves states the checker cannot
reconcile:

| Metadata | Write path |
|---|---|
| block and inode bitmaps | `write_metadata_blocks`, one block per transaction |
| group descriptors | descriptor segment, then one transaction per block |
| primary superblock | `write_bytes`, outside the journal entirely |
| inode table | page-cache writeback to the home block |

The fix follows jbd2: **one transaction covers the allocation set.** A
`MetadataTxn` stages blocks by block number; `Ext2::sync_all` flushes data
first (`data=ordered`), stages the inode table, the bitmaps, the group
descriptor table and the primary superblock, then commits all of them with one
`write_metadata_blocks` call. Indirect blocks, xattr blocks and `fsync` use the
same staging path. Backup superblocks are written after the commit; they are
advisory and the checker reads the primary.

Two ordering details matter more than the mechanism:

- The inode table is staged **before** the bitmap snapshot. An allocation that
  races with the pass can then only leave a bitmap bit without a descriptor
  (a leaked inode) — never a descriptor without the bit that authorises it.
- Remounting read-only now flushes the filesystem first, as Linux'
  `do_remount_sb` does. Without it, the final sync of a shutdown runs with
  `RDONLY` already set and every metadata write is refused with `EROFS`.

**Every commit carries the whole allocation set.** The first version of the
transaction still failed fault injection: an `fsync` staged one inode-table
block and published it without the bitmaps, so replay restored a descriptor
whose allocation bits had never been written. Every commit now runs the
filesystem-wide staging pass (data, descriptors, inode tables, bitmaps, group
descriptors, primary superblock) before writing the commit record, and `fsync`
uses the same path. A lighter `fsync` that only flushes the calling inode was
tried and rejected by fault injection: a socket of concurrent work can allocate
a block after its inode was flushed but before the bitmap snapshot, leaving an
allocated-but-unreferenced block. Closing that needs per-operation transaction
handles, not a cheaper barrier.

The journal also needed the `RECOVER` incompatible feature to be managed
explicitly: it is set when a writable filesystem starts using its journal and
cleared by the sync that follows a read-only remount. Without the bit, neither
e2fsck nor this kernel's mount path looks at the journal at all, so a power
loss during the home-block writes is accepted as a valid state.

### Acceptance (2026-10-18)

`tools/x86/debian/fsck_acceptance.sh` drives three matrices, each run starting
from the same clean rootfs image:

| Matrix | Runs | Result |
|---|---|---|
| clean shutdown, then `e2fsck -fn` | 3 | clean every time |
| `SIGKILL` after 150 s, then replay and `e2fsck -fn` | 3 | clean, no structural repairs |
| fault injection at commit stage 1 / 2 / 3, then replay and `e2fsck -fn` | 1 each | clean, no structural repairs |

The fault stages are: after the journal commit record is durable, halfway
through the home-block writes, and after every home block is written but
before the checkpoint. In all seven runs the final check found no unattached
inodes, no bitmap differences, and no xattr or `i_blocks` disagreements. The
three clean-shutdown images report the same inode and block counts
(`19478 files, 327041 blocks`), which is what a deterministic userspace writing
the same files should produce.


The journal descriptor encoder also emitted its tags eight bytes apart without
the sixteen-byte filesystem UUID, so any transaction with more than one block
was unreadable to both jbd2 and this module's own parser. It now writes the
UUID once after the first tag and sets `FLAG_SAME_UUID` on the rest. The
transaction builder also rejected block zero, which is where the primary
superblock lives with 4 KiB blocks.

**What working syncs then exposed.** Once metadata actually reached the disk,
the checker reported two long-standing defects in the xattr writer on
`/var/lib/systemd/random-seed`:

```
Extended attribute in inode 19448 has a hash (0) which is invalid
Inode 19448, i_blocks is 8, should be 16
```

The entry hash was hardcoded to zero. ext4 uses the legacy
`ext4_xattr_hash_entry` rotation over the name and value words, and folds the
entry hashes into the block header with the same 16-bit rotation; both are now
computed (verified against reference values produced by the kernel on a
filesystem without metadata checksums). `i_blocks` also omitted the xattr block
for every inode that is not a fast symlink; it is now counted for all payload
types.

## Storage persistence: writes survive reboots and crashes

The milestone above made `e2fsck` accept the image after a clean or crashed
shutdown, but it never checked that the system could **boot again**, and it
never checked that the data a guest wrote came back unchanged. Two gates were
added:

- `tools/x86/debian/persistence_acceptance.sh` boots one image **repeatedly**.
  On every boot the guest writes a small file, a file that crosses a block
  boundary, 1 MiB of random data, a nested directory, a hard link, a symlink, a
  0640 mode, and an append-only log; it writes a manifest with SHA-256 sums,
  fsyncs the files and directories, `sync`s, and shuts down cleanly. After each
  boot the host re-dumps every file written by **this and all earlier boots**
  from the image and compares the sums, checks the cumulative counter and log,
  and runs `e2fsck -fn`.
- A crash test boots with `asterinas.ext4_fault_stage=1` (halt after the
  journal commit record, before the home blocks), kills the VM, and then boots
  the same image again. Recovery must replay the committed transaction, finish
  a normal boot, and leave `e2fsck -fn` clean.

Both failed on their first run, and the failures were independent bugs.

**1. A recovery-pending filesystem could not be mounted at all.** The
superblock validator allowed only the `FILETYPE` and `EXTENTS` incompatible
features, but `RECOVER` is an incompatible feature too: it marks a journal that
must be replayed. Every writable mount sets it, so the *second* boot of any
image failed with `mount(2) = EINVAL` (Stage1: `handoff-failed action=root-mount`,
then a hung VM) before the replay path was ever reached. `RECOVER` is now in the
allowed set; the mount path still decides whether a replay is needed.

**2. A clean shutdown never cleared `RECOVER`.** `sync_all` only clears the
flag when the filesystem is already marked read-only, and the remount path
flushed *before* setting `RDONLY` (necessarily, because metadata writes are
refused after). systemd's `Syncing → Remounting '/' read-only → Syncing`
sequence therefore tried a second sync that failed with `EROFS` before reaching
the clear. The VFS now performs one final sync after switching to read-only, and
`sync_all` in that state only finalizes the journal instead of staging
metadata.

**3. The xattr block was counted twice in `i_blocks`.** The block manager was
initialised from the on-disk `i_blocks`, which already includes the separate
xattr block, and `raw_block_ptrs()` adds that block again on write-back. Each
boot grew `i_blocks` by eight sectors (`Inode 19448, i_blocks is 24, should be
16`, then 32 on the next boot). The manager now starts from the data-and-tree
only. The same double count existed in the fast-symlink-to-data conversion and
was fixed with it.

**4. The uninitialised tail of the journal ring was treated as corruption.**
`scan_log` read the whole ring starting at `s_start` and errored on the first
block without a JBD2 magic; jbd2 treats that block as the end of the log. A
crash image therefore failed recovery with `EUCLEAN` even though its committed
transaction was intact (`debugfs logdump` parsed it). A block without the magic
now ends the scan unless it is the very first block.

Measured after the fixes (same image, loglevel 4 unless noted):

```
persistence_acceptance.sh --boots 3
  boot 1: writes durable, earlier boots intact; e2fsck -fn clean
  boot 2: writes durable, earlier boots intact; e2fsck -fn clean
  boot 3: writes durable, earlier boots intact; e2fsck -fn clean
  final image: no needs_recovery, boot-count=3, log lines 1-3,
               inode 19448 i_blocks=16 (stable), e2fsck -fn clean

fault stage 1 crash, then boot the same image:
  mount succeeds, journal replay runs, the guest reaches multi-user,
  the probe writes boot 4 and powers off cleanly,
  final image: no needs_recovery, boot-count=4, e2fsck -fn clean
```

The Stage1 handoff now also prints the `mount(2)` errno
(`DEBIAN_ROOT_MOUNT ext4 errno=N`) and the ext2 mount path logs the failing
reason, which is what made the first bug visible.

## Socket visibility: `/proc/net` and `NETLINK_SOCK_DIAG`

`ss`, `ip`, and the Debian boot scripts expect to see the socket table. The
kernel had neither text nor netlink views: `/proc/net` only contained `dev`,
`route` and `arp`, and `NETLINK_SOCK_DIAG` was refused at `socket()`. Both
views now read one registry of live IPv4/IPv6 sockets, so a row in `ss` and a
row in `/proc/net/tcp` describe the same socket.

The registry lives in `net/socket/ip/registry.rs`. Sockets register when they
are created (accepted connections included) together with the sockfs inode
number that `/proc/<pid>/fd` renders as `socket:[N]`, and the entries hold weak
references so a closed socket disappears on the next enumeration.
`/proc/<pid>/net` gains `tcp`, `tcp6`, `udp` and `udp6` with Linux's state
codes and native-endian hex address layout.

`NETLINK_SOCK_DIAG` reuses the netlink socket machinery: the protocol table
gains a `SockDiagMessage`, the bound socket parses `SOCK_DIAG_BY_FAMILY`
requests, and the handler filters the registry by family, protocol and the
`idiag_states` mask before appending `NLMSG_DONE`. The state mask sets bit
`sk_state` (not `sk_state - 1`), which is what makes `ss -l` select listening
sockets while `ss -a` selects everything.

Measured in the guest with four sockets created by Python (a TCP listener, a
TCP connection pair, a bound UDP socket and a connected UDP socket):

```
$ ss -tan
State  Recv-Q Send-Q Local Address:Port  Peer Address:Port
LISTEN 0      0          127.0.0.1:45678       0.0.0.0:*
ESTAB  0      0          127.0.0.1:32768     127.0.0.1:45678
ESTAB  0      0          127.0.0.1:45678     127.0.0.1:32768
$ ss -uanp
UNCONN 0      0            0.0.0.0:45679       0.0.0.0:*    users:(("python3",pid=72,fd=4))
ESTAB  0      0          127.0.0.1:45680     127.0.0.1:45679 users:(("python3",pid=72,fd=7))
```

`ss -tln` lists only the listener, and `/proc/net/tcp` reports the listener as
`0A` (LISTEN) and both connection halves as `01` (ESTABLISHED). Process
attribution (`-p`) works because the reported inode is the one the fd symlink
carries.

### Registering an accepted socket could panic in atomic mode

`try_accept` holds the listener's `state` read guard (`PreemptDisabled`) and
`self.options.read()` while it builds the accepted socket, and
`new_accepted` registered that socket before returning. The registry mutex is
a sleeping lock; its `lock()` only calls `might_sleep` on the contended slow
path, so the fault appeared as a rare panic whenever another task held the
registry at that instant (`/proc/net/*` or a `NETLINK_SOCK_DIAG` dump, or a
concurrent accept):

```
Uncaught panic: This function might break atomic mode
  (preempt_count = 2, is_local_irq_enabled = true)
  at kernel/core/src/net/socket/ip/registry.rs:80
```

One full-suite run died this way 166 s in, exiting with the machine halted.
`new_accepted` no longer registers; `try_accept` registers the accepted
socket after `drop(state)`, outside both guards. Five full-suite runs after
the fix saw no panic.

## Loopback latency: TCG, not the kernel

The RPC measurements were dominated by a ~10 ms loopback round trip. A
three-layer probe separates the possible causes: a same-thread UDP
send-to-self (kernel data path plus two syscalls), a UDP echo answered by a
second thread, and a TCP echo. Both accelerators run the same kernel and the
same Debian image:

| p50 round trip | TCG | KVM |
|---|---|---|
| same-thread UDP | 6.86 ms | 30 µs |
| thread UDP echo | 14.07 ms | 102 µs |
| thread TCP echo | 20.22 ms | 182 µs |

Even the same-thread case, which involves no scheduler wakeup, is measured in
milliseconds under TCG. Loopback delivery is synchronous inside `send` (the
loopback device never carries a packet), so the delay is emulation cost, not
the interface poll scheduler. KVM shows a healthy path: 30 µs for a
same-thread UDP round trip and ~100-180 µs for an echo.

This also explains the RPC failures: libtirpc retries after 2.5 ms, which is
shorter than a single TCG syscall pair. Under KVM the round trip is two orders
of magnitude below that window, so the retry path is not exercised.

## KVM: a missing reschedule on cross-CPU wake-ups

The TCG numbers hid a second, kernel-side cause. Probes under KVM still saw a
~1 ms loopback round trip, but only in about half of the trials and then
uniformly across the whole trial; the other trials were tens of microseconds.
Which leg was slow moved between probes.

A timestamped two-process UDP ping-pong separated the round trip into request,
server-processing, and reply legs. `CLOCK_MONOTONIC` is shared by both
processes, so the split is direct. In the slow trials the send itself was fast
(53 µs in the kernel, measured by the sender) and a woken process resumed
within microseconds of its wake-up; the wake-up was what arrived late, up to a
timer tick after the datagram was delivered. Delivery is synchronous inside
the sender's `send` (the loopback device never carries a packet), so the whole
delay was in "enqueue the woken task and let it run".

Root cause: `ClassScheduler::enqueue` requested a reschedule only when the
woken task had a higher scheduling-policy priority than the target CPU's
current task. A wake-up that landed on another CPU running an ordinary fair
task did not kick that CPU, and the woken task waited for the next timer tick.
Waking an idle CPU did preempt (fair outranks idle), which is why some trials
were fast: whether a leg cost 23 µs or ~900 µs depended on what the target CPU
happened to be running.

The first fix requested a reschedule whenever a wake-up landed on a remote
CPU. It was later widened to every wake-up, and the scheduler was changed to
run the woken task itself rather than the run queue's leftmost entity; see
"`rpc-udp` after the 2026-10-19 fixes" below.

| two-process UDP ping-pong, 30 calls/trial, 2 vCPU KVM | before | after |
|---|---|---|
| request leg, p50 | 23 µs or ~900 µs, whole-trial | 23 µs |
| reply leg, p50 | 23 µs or ~900 µs, whole-trial | 24 µs |

With the fix, libtirpc's `lat_rpc -p udp` calls complete in 65-105 µs and run
thousands of times back-to-back. Before the fix the same calls took ~4 ms and
the client exhausted its 25 ms virtual budget after a few dozen calls, because
every reply that missed the 2.5 ms retry window produced a duplicate reply and
the duplicate then burned 2 ms of the budget on each later call.

That mechanism is what makes `rpc-udp` fragile, but it is not the whole story:
with the wake fix the benchmark still failed, just much later (about 20,000
calls instead of a few hundred). The remaining trigger is tail latency:
libtirpc retransmits when a reply does not arrive within 2 ms, and a woken
task that lands on a CPU which is busy in a long kernel operation can wait
longer than that. `select_cpu` always placed a woken task back on its previous
CPU, even when another CPU was idle. The scheduler now prefers an idle CPU
when the previous one is busy, which pushed the failure from ~400 calls to
~20,000 and brought the slow-call tail from ~2 ms down to ~1.1 ms.

## A user page fault under a socket lock panicked the kernel

With the wake-up fix in place the suite got past the build phase, and a second,
independent defect surfaced: the guest panicked about 3.5 minutes into `make
results`.

```
Uncaught panic: This function might break atomic mode
    (preempt_count = 3, is_local_irq_enabled = true)
    at kernel/core/comps/block/src/bio.rs:314
```

`might_sleep` fires when a task blocks while preemption is disabled. The panic
stack ends in a page fault that the kernel took in kernel mode:

```
trap_handler (page fault, cs = kernel code)
→ handle_user_page_fault → handle_page_fault_from_vmar
→ VmMapping::handle_page_fault → handle_page_faults_around
→ MappedVmo::commit_on → BackedVmo::commit_on_internal
→ PageCacheBackend::read_page → IoBatch::wait_all → bio wait
```

The interrupted kernel stack is not reachable from the fault handler's
unwinder (it stops at the trap frame), so a temporary diagnostic scanned the
kernel stack from the trap frame and wrote the return addresses to QEMU's
debug console, port 0xe9. That showed the fault came from
`ConnectedStream::try_send` → `aster-bigtcp`'s `Socket::send` →
`smoltcp::Socket::send` → `MultiRead::read`: the kernel was copying a user
buffer **while holding the interface lock and the socket lock**.
`StreamSocket::try_send` adds a third, because its `state` field is an
`RwLock` (a spinning reader-writer lock). Hence `preempt_count = 3`.

The source buffer was a string constant: the faulting address was in a
file-backed `.rodata` mapping whose page had not been faulted in yet. Serving
that fault means reading the page from the ext2 image, which blocks — illegal
in atomic mode. The UDP send path has the same shape (its copy also runs
inside `Socket::send` under the same locks).

The fix keeps user memory out of the atomic region:

- `StreamSocket::try_send` faults the pages in (`MultiRead::prefault`, one
  read per page with faults enabled) **before** taking the state lock, then
  copies with the page fault handler disabled, so a buffer remapped
  concurrently fails with `EFAULT` instead of panicking.
- The UDP send path stages the datagram in a kernel buffer before taking the
  socket lock. That also removes the old FIXME where a failed copy still sent
  a partial datagram.

With the fix the suite runs to the end: 108 of 109 measurements,
`elapsed_seconds: 573.7`, and UDP/TCP/RPC-tcp latencies of 107.6 µs /
104.9 µs / 155.0 µs. Only `rpc-udp` is still missing.

## `systemd` and `systemd-journald` were spinning inside `epoll`

Sampling `/proc/[pid]/stat` showed that the guest was never idle, even with no
benchmark running:

```
10 s of idle after boot:  systemd-journald +8434 jiffies (84% CPU)
                          systemd         +1679 jiffies (17%)
10 s idle with rpcbind:   rpcbind         +5232 jiffies (52%)
                          systemd-journal +5202 jiffies
a running rpcbench server:+53 jiffies (0.5%)
```

A static probe (`/opt/waittest`) exercised the readiness primitives. A
`timerfd` that had already fired and been read was reported ready forever:

```
epoll_empty_1s                1001 ms  ok
epoll_timerfd_100ms            101 ms  ok
poll_empty_1s                 1000 ms  ok
poll_timerfd_100ms               0 ms  BUG: ready at once
epoll_udp_after_drain_200ms      0 ms  BUG: ready after the data was drained
epoll_wait(timeout=0) x 1000  1000/1000 ready   BUG
```

The timerfd's read path consumed the expiration counter but never invalidated
its `Pollee` cache. `Pollee::poll_with` caches the computed event set and only
`notify()` or `invalidate()` drop the cache. The cached `IN` therefore survived
the read, and a level-triggered `epoll` reported the timerfd readable forever.
`sd-event` (systemd, journald) uses timerfds for its timers, so both daemons
spun in `epoll_wait`.

The fix invalidates the pollee after consuming the counter in
`TimerfdFile::try_read` and after clearing it in `set_time`. After the fix the
probe passes every check:

```
poll_timerfd_100ms            101 ms
epoll_udp_after_drain_200ms   201 ms, timed out
epoll_wait(timeout=0) x 1000    0/1000 ready
idle CPU: journald +2 jiffies / 5 s, systemd +0
```

The guest is now genuinely idle when idle. Every timing-sensitive benchmark
in the suite benefits; the numbers in the section above were all measured with
the daemons spinning.

## Running the native LMbench ALL suite

`python3 tools/riscv/lmbench_native.py package --architecture amd64` builds the
runtime archive; `tools/x86/debian/prepare_lmbench_disk.sh` copies a published
rootfs and installs it, so the guest only runs the benchmark. The Kbuild results
come from the upstream driver's own `make results`, reached through
`asterinas-native.py`.

Observed on this kernel under KVM, 4 vCPU (`-cpu host`, `--accel kvm`):

```
LMBENCH_RUN_NATIVE_EXIT 2 <ts>      make exit 2 (the coverage gate, not a crash)
  "native_exit_status": 0,          the inner make itself succeeded
  "timed_out": false,
  "elapsed_seconds": 573.7,
  observed: 108 of 109
```

Four defects had to be fixed to get there; each was measured, not inferred.

- **Nix-linked binaries died at startup.** That was the image above.
- **`config-run` aborted before writing any CONFIG.** It runs `memsize` three
  times and takes `MB < 8` as "not enough memory"; `memsize` does not measure
  free memory but walks a growing range and stops once the average per-page
  touch exceeds its fixed 10 usc threshold, which TCG trips. `MemTotal` reads
  correctly on the same guest. The run died after 34 s with an empty report.
- **`tlb` and `memory-parallelism` printed their usage line.** `scripts/lmbench`
  invokes `tlb -L $LINE_SIZE -M ${MB}M`; with `LINE_SIZE` empty, `-L` swallows
  `-M`. Upstream defaults `LINE_SIZE` only when there is no CONFIG, and with one
  it comes solely from config-run's `line -M ${MB}M`, whose timing probe can
  return nothing here — one run wrote `LINE_SIZE=` and the next wrote `128`.
- **`useradd --system rpc` failed.** Debian's base image ships an `rpc` group
  but no `rpc` user, so useradd tried to create a same-named group, found it,
  and exited without creating the user; the runner's `getpwnam('rpc')` then
  aborted the whole run after 0.5 s.

Measured before and after, same disk and command: 0 of 109 at 34 s (TCG), then
105 of 109 at 2235 s (TCG). After the KVM wake-up, socket page-fault and
scheduler fixes described above, the suite reaches 108 of 109 in 574 s under
KVM (`signal-prot` was missing on the preceding run, which measured 107). The runner's deadline also had to be raised — its 900 s default cut the
run off at 905 s having reached 73 of 109 — so the unit sets
`LMBENCH_TIMEOUT=3600`.

### What is still missing

After the 2026-10-19 fixes (idle daemons, wake-next scheduling, precise
timeouts, accepted-socket registration), five consecutive full-suite runs of
the same image on the same kernel (4 vCPU, KVM, loglevel 4, no host pinning,
host governor `powersave`) report:

| run | transcript | verdict | observed | missing | elapsed |
|-----|------------|---------|----------|---------|---------|
| 1 | `lmbench-clean3-1` | passed | 109/109 | - | 572.0 s |
| 2 | `lmbench-clean3-2` | passed | 109/109 | - | 558.2 s |
| 3 | `lmbench-clean3-3` | passed | 109/109 | - | 562.2 s |
| 4 | `lmbench-clean3-4` | **failed** | 108/109 | `signal-prot` | 574.0 s |
| 5 | `lmbench-clean3-5` | passed | 109/109 | - | 564.3 s |

`rpc-udp` passed in all five runs — the wake-next, exact-deadline and timerfd
fixes hold in the full suite. The only remaining gap is `signal-prot`, which
is missing in one run in four. The failing run is not a crash: the process
tree exits 0 and the runner's inner `make` succeeds.

The same five-run check after the signal-frame fix (kernel `clean5`, 4 vCPU
pinned to P-cores 0,2,4,6, `performance` governor) reports:

| run | transcript | verdict | observed | missing | elapsed |
|-----|------------|---------|----------|---------|---------|
| 1 | `lmbench-clean5p-1` | passed | 109/109 | - | 561.1 s |
| 2 | `lmbench-clean5p-2` | passed | 109/109 | - | 581.3 s |
| 3 | `lmbench-clean5p-3` | passed | 109/109 | - | 564.4 s |
| 4 | `lmbench-clean5p-4` | **failed** | 108/109 | `signal-prot` | 556.4 s |
| 5 | `lmbench-clean5p-5` | **failed** | 108/109 | `signal-prot` | 573.9 s |

The same kernel run five times in the baseline configuration (unpinned,
`powersave`) gives:

| run | transcript | verdict | observed | missing | elapsed |
|-----|------------|---------|----------|---------|---------|
| 1 | `lmbench-clean5-1` | passed | 109/109 | - | 565.6 s |
| 2 | `lmbench-clean5-2` | **failed** | 108/109 | `signal-prot` | 579.4 s |
| 3 | `lmbench-clean5-3` | passed | 109/109 | - | 564.4 s |
| 4 | `lmbench-clean5-4` | passed | 109/109 | - | 566.1 s |
| 5 | `lmbench-clean5-5` | **failed** | 108/109 | `signal-prot` | 578.6 s |

So 4/5 in the baseline configuration and 3/5 pinned, with the missing group
always the same. A second default-configuration set (`lmbench-clean5b-1` to
`-5`) gave 3/5 again (pass, pass, miss, miss, pass). Across all four sets
that is 13 green runs out of 20; nothing except `signal-prot` was ever
missing. The outcome of this single measurement is set by the host state
during the run (see the next subsection), not by the kernel version: the
signal-frame fix removes 15% of the signal cost but does not change the
rate, because the margin the benchmark measures is independent of it.

The 20-invocation quick indicator on the fixed kernel in the same
configuration passed 19 of 20 standalone `lat_sig prot` runs.

A fifth set added `-overcommit cpu-pm=on` to QEMU (P-cores pinned,
`performance`). Its 20-invocation probe passed 20/20 and `rpc-udp` 5/5, and
the suite set came out 4/5: runs 1, 2, 4, 5 passed, run 3 missed
`signal-prot` in the fast state (catch 8.22 us). Across five sets and 25
full-suite runs, 17 were green and every one of the 8 missing groups was
`signal-prot`; no set reached 5/5. `cpu-pm=on` is the best-measured
configuration but it does not change the mechanism.

#### Reading the transcripts: the run directories are stale

The guest dumps every directory under `/opt/lmbench/asterinas-runs` at the end
of a run, and the image retains directories from earlier boots (the guest's
ext4 writes are not a reliable channel). A transcript therefore contains
several `report.json` dumps, most of them from previous runs. Identify the
run's own report by matching its `elapsed_seconds` against the console's
`LMBENCH_RUN_NATIVE_START` / `LMBENCH_RUN_NATIVE_EXIT` difference; in the five
runs above that match is within 1.0 s, while stale reports differ by 5-130 s.

`passed` is still false in the aggregate because of that single missing
measurement, and the gate was not loosened to change it.

### `rpc-udp`: a 2 ms retry window against a millisecond-scale wake tail

Once the wake-up fix above landed, loopback RPC calls take 65-105 µs and
`lat_udp` measures 86.3 µs. `lat_rpc -p udp` still fails: libtirpc retransmits
a call whose reply has not arrived within 2 ms (`CLSET_RETRY_TIMEOUT` is
2.5 ms, truncated to 2 ms by `nextsend_time`), and each retransmission leaves
a duplicate reply in the client's socket. `clnt_dg_call` charges every XID
mismatch 2 ms of its virtual budget without sleeping; once one retransmission
has happened, later calls keep finding the stale duplicate, retransmitting,
and adding duplicates, until one call exhausts its 25 ms budget and exits with
`RPC_TIMEDOUT`.

An `LD_PRELOAD` hook wrapped `clnt_call`, `poll` and `recvfrom` to measure
this directly. Every slow call coincides with a poll timeout, with no spurious
readability and no `EAGAIN` from `recvfrom`, and the failure arrives a few
calls after the first reply that misses the window. The first trigger is a
wake-up that lands on a busy CPU: `select_cpu` always put a woken task back on
its previous CPU, even when another CPU was idle, so a reply could wait for
that CPU to reach a scheduling point.

The scheduler now prefers an idle CPU when the previous one is busy. With the
same hook, `lat_rpc -P 1 -p udp` then survived about 20,000 calls instead of
400, and the slow-call tail fell from ~2 ms to ~1.1 ms. The remaining tail is
the 1 ms timer tick (`TIMER_FREQ = 1000`) and rare millisecond-scale kernel
operations (synchronous ext2 commits); when every CPU is busy at the wake
instant a reply can still miss the window. Whether the remainder is guest
scheduling or host vCPU preemption is not settled yet.

Two earlier probes were dead ends worth recording. Asking the portmapper for
`XACT_PROG` version 2 returns port 0 because LMbench registers version 1, so
that probe always says nothing. And a bare `lat_rpc -s` is identical to
`-s 127.0.0.1`: the source forks, runs `server_main()` in the child and has the
parent `exit(0)` from inside `getopt`, so nothing after `-s` is ever read.

### `rpc-udp` after the 2026-10-19 fixes

Three further changes were made for this measurement:

- **A wake-up now runs the woken task next.** The fair scheduler used to pick
  the globally smallest virtual runtime after a reschedule, which could be an
  unrelated task that had slept for a long time. A `Wake` enqueue now records
  the woken task in the run queue (`FairClassRq::note_wake_candidate`, the
  equivalent of Linux's `cfs_rq->next`) and `pick_next` returns it for the
  next scheduling decision. A wake-up also always requests a reschedule, and
  a wake-up that finds the task already queued requests one as well.
  Enqueue-to-dispatch latency over 1 ms dropped from ~220 to ~10 per trial.
- **Timeout deadlines are exact.** `ManagedTimeout` froze deadlines against
  the jiffies clock (1 ms granularity), so a `poll(2 ms)` could expire after
  1 ms. It now uses the CLOCK_MONOTONIC timer manager (ns resolution); a
  timeout can no longer fire early.
- **The timerfd fix above** removed the daemon contention.

Measured with `lat_rpc -P 1 -p udp` (ENOUGH=10000, 1 vCPU): the client still
collects exactly 13 poll timeouts per trial and the benchmark still ends with
`RPC: Timed out` (5 of 5 plain runs, no `LD_PRELOAD`). The system is idle at
that point, so this is not load.

Tracing both peers together shows the remaining failures are lost wake-ups,
in both directions:

- the server receives a request 21 us after the send and sends the reply
  82 us after it, yet the client's `poll(2 ms)` sleeps to its deadline;
- a server `poll(-1)` started 230 us before a request only returns 3113 us
  later, woken by the client's retransmission rather than by the original
  delivery.

The wake-up path is instrumented (temporarily) with counters; every wake
carries an event (`poll_empty` is zero), so this is not a spurious-wakeup
problem, and the wake-up-to-dispatch latency is small. The remaining loss is
in the poll/wake-up protocol itself, not in delivery (a second interface poll
in `try_send` changed nothing) and not in timer accuracy. The next step is to
instrument `Pollee::poll_with` / `Waker::wake_up` with the poller's registered
mask at the notify instant. This one is still open.

### `signal-prot`: the difference of two 9 us measurements is the margin

`lat_sig -P 1 prot lat_sig` prints no `Protection fault` line on roughly one
invocation in five. It is not a lost wake-up and not an empty benchmp result
set: the handshake always completes and the child always writes its 184-byte
result buffer. The line is suppressed by the benchmark itself:

```c
micro(char *s, uint64 n) {
    micro = (stop - start) / n;
    if (micro == 0.0) return;        /* prints nothing */
    fprintf(ftiming, "%s: %.4f microseconds\n", s, micro);
}
```

and `bench_prot` computes

```c
bench_catch(...);                    /* catch_usecs, catch_n, silent */
benchmp(initialize, do_prot, ...);
if (gettime() > catch_usecs * get_n() / catch_n)
        settime(gettime() - catch_usecs * get_n() / catch_n);
else
        settime(0);                  /* -> micro() prints nothing */
```

So the measurement is missing exactly when

    prot_median - (catch_median - send_median) <= 0     (integer microseconds)

`/tmp/sigprobe` reproduces this arithmetic independently (same handler
structure, its own clock). On the host it gives send 0.137 us, catch
1.183 us, prot 1.513 us per call, i.e. +0.45 us, while the unmodified
`lat_sig -P 1 prot` prints +0.46 us. In the guest (4 vCPU, KVM, pinned to
P-cores at the performance governor):

| n | send | catch | prot | margin |
|---|------|-------|------|--------|
| 300 | 0.565 us | 8.109 us | 7.724 us | +0.18 us |
| 3000 | 0.564 us | 8.058 us | 7.794 us | +0.30 us |

The margin is essentially `send - (catch - prot)`: the self-`kill` syscall
cost minus a small fault penalty. That is a structural property of the signal
and page-fault paths, not a race, and there is no kernel defect behind it.

What flips the sign is measurement stability, not correctness. The three
costs are measured by three independently calibrated child processes at
different moments. The host is a hybrid Intel i5-1340P (P-cores 0-7 at
4.6 GHz, E-cores 8-15 at 3.4 GHz) running the `powersave` governor; in an
unpinned run the `send` samples jump from 1656 us to 2588 us (+56%) in the
middle of one measurement, consistent with a P-to-E or frequency move.
Pinning QEMU to P-cores (`taskset -c 0,2,4,6`) and switching to the
performance governor removes the jump (send 1670-1710 us), but standalone
`lat_sig prot` is still 10 of 12, because the children can still land on
different physical cores.

An `LD_PRELOAD` trace that dumps the exact 184-byte result buffers confirms
the model to the last digit. Replaying bench_prot's integer arithmetic on one
traced trial (send 10860/16713, catch 10693/1120, prot 11166/1182) gives
650 us / 1182 = 0.5474 us, which is exactly what `lat_sig` printed.

The suite stores one `Protection fault` line per run in `native-results.txt`,
so one negative draw removes the group. The five-run baseline above is four
green and one missing for that reason.

#### CPU-count and host-placement bisect

The same probe (12 standalone `lat_sig -P 1 prot` trials and 5 `lat_rpc -p udp`
trials) was run at each CPU count and host placement. `taskset` pins the QEMU
process; on this machine CPUs 0-7 are P-cores (4.6 GHz, two SMT threads each)
and 8-15 are E-cores (3.4 GHz).

| guest CPUs | host pinning | `signal-prot` | `rpc-udp` |
|------------|--------------|---------------|-----------|
| 4 | none, `powersave` | 10/12 | 5/5 |
| 4 | P-cores 0,2,4,6, `performance` | 10/12 | 5/5 |
| 4 | P-core threads 0-7 | 9/12 | 5/5 |
| 2 | P-cores 0,2 | 10/12 | 5/5 (78 us) |
| 2 | SMT siblings 0,1 | **12/12** | 5/5 (probe), but the full suite run failed `rpc-udp` |
| 1 | P-core 0 | **12/12** | **0/5** |

`signal-prot` is stable only when the benchmark's three child processes see
identical timing conditions (one core), and `rpc-udp` needs at least two CPUs
with enough headroom. The suite cannot satisfy both at once. Making the child
processes inherit the forking CPU (Linux wake-affine placement) was
implemented, measured (still 10/12) and reverted.

The suite's own results show why a per-run coin flip is structural rather than
a defect. Under `powersave` the recorded `catch` cost ranged from 10.3 us to
25.4 us between runs, and the printed `prot` margin moved in the same
direction: catch 11.0 -> prot 0.66, catch 11.4 -> prot 2.16, catch 23.6 ->
prot 12.9, catch 25.4 -> prot 13.5. The missing run is the fastest of the five
(catch 10.28 us): at that point the subtraction is smaller than the
run-to-run variation of the two terms.

A decomposition probe (`/tmp/sigprobe`, n=3000, per call) separates what the
8 us signal cost is made of:

| operation | per call |
|-----------|----------|
| `kill(pid, 0)` | 0.56 us |
| `kill(pid, SIGUSR1)` with `SIG_IGN` (queued and discarded, no frame) | 1.69 us |
| `kill(pid, SIGUSR1)` with a handler | 8.07 us |

Frame construction, handler entry and `rt_sigreturn` therefore account for
~6.4 us (79%) of every delivered signal; two user/kernel transitions are
~2.8 us of that.

#### The frame path: FPU reset removal

Instrumenting `handle_user_signal` with per-stage TSC marks (temporarily
exposed through `/proc/stat`) located the frame cost:

| stage | ns/signal |
|-------|-----------|
| mask and stack setup | 150 |
| `siginfo_t` write | 135 |
| `ucontext_t` build | 95 |
| **FPU context get** | **964** |
| **FPU context set (reset)** | **1096** |
| FPU context write to the user stack | 102 |
| `ucontext_t` write | 106 |
| restorer/register setup | 208 |
| total | 2856 |

The FPU handling was 72% of the frame cost. Part of it was unnecessary: the
handler was given a freshly allocated, zeroed FPU context
(`set(FpuContext::new())`). Linux does not reset the FPU state for a signal
handler, and `rt_sigreturn` restores the saved state anyway. Removing the
reset also lets `before_user_exec` skip an `XRSTOR`, because the CPU already
holds the value. The handler now sees the interrupted FPU state, as on Linux.

A second, larger cut was measured but not shipped. `CpuSync::get()` clones
the whole XSAVE area (a heap allocation plus a multi-kilobyte copy) only to
read its length and write it to the user stack. A `with_current` variant that
runs the write closure on the canonical value without cloning brought
`handle_user_signal` from 2856 ns to 990 ns per signal and the full
`kill`+deliver+return loop from 8.19 us to 5.72 us, with the standalone
`lat_sig prot` probe at 12/12. It is not shipped because the closure holds
the FPU context borrowed across a user-stack write: a page fault there can
block, switch the task out, and reach `before_schedule`, which saves the same
context — a `RefCell` double-borrow panic. The shipped version keeps the
safe snapshot and only removes the reset.

The five-run verification of the shipped change is the `clean5` table in
"What is still missing".

#### The residual sign flip is host scheduling, not the guest

Three configurations were measured with the fixed kernel, and every
`signal-prot` outcome correlates with the speed of the run. The two states
differ by 2.6x even with the `performance` governor, which rules out frequency
scaling; they are the laptop's sustained-load power/thermal limit and host
vCPU preemption.

| run | catch (`Signal handler overhead`) | `Protection fault` | verdict |
|-----|-----------------------------------|--------------------|---------|
| clean5p-3 | 21.25 us | 10.89 us | pass |
| clean5p-4 | 8.18 us | - | missing |
| clean5p-5 | 7.87 us | - | missing |
| clean5 unpinned-1 | 22.42 us | 12.18 us | pass |
| clean5 unpinned-2 | 8.30 us | - | missing |
| clean3-2 | 23.65 us | 12.94 us | pass |
| clean3-3 | 25.44 us | 13.45 us | pass |
| clean3-4 | 10.28 us | - | missing |

In the slow state the margin is large in absolute terms, and the subtraction
is stable. In the fast state the margin is ~0.2 us, comparable to the
host-induced jitter of either term, and the sign is a coin flip. A
four-vCPU run pinned to the E-cores reproduces the slow state (catch 24 us)
with a stable 12/12, but inflates every latency by 3x, so it is not a valid
performance configuration.

The kernel work is therefore complete: there is no defect left to fix in the
signal path. Passing `signal-prot` reliably in the fast state would require
isolating the host (cpuset partition or offlining SMT siblings and moving
every other host process off the measured cores), which is an environment
change, not a kernel fix.

One host-isolation attempt was made. A cgroup v2 cpuset with
`cpuset.cpus=0,2,4,6` and `cpuset.cpus.partition=isolated` gives QEMU
exclusive use of four P-cores. `signal-prot` then becomes deterministic
(fast state, catch 6.98 us, 12/12 twice), but `rpc-udp` fails five of five
calls in both attempts. The partition restricts every task in the root
cgroup, including `ksoftirqd`, `kworker` and RCU threads, so the host's
per-CPU work for the isolated CPUs is starved and KVM's work on those CPUs
stalls. A usable isolated setup needs `isolcpus` plus explicit kthread
affinity, which is a boot-level host configuration; the cgroup was removed
and the host left as it was.

## Reproducing

```sh
# 1. A Debian root image for amd64.
tools/riscv/debian/rootfs/build_rootfs.sh --architecture amd64 \
  --profile systemd-ext4-m6-userspace --output-dir target/debian-x86/m6/rootfs

# 2. The Stage1 initramfs for amd64.
tools/riscv/debian/rootfs/build_stage1.sh --architecture amd64 --minimal \
  target/debian-x86/stage1/initramfs.cpio

# 3. An x86_64 kernel OSDK can hand to QEMU directly.
(cd kernel && cargo osdk build --target-arch x86_64 --boot-method vmm-direct \
  --initramfs ../../target/debian-x86/stage1/initramfs.cpio)

# 4. Boot it.
tools/x86/debian/boot_debian.sh \
  --kernel target/osdk/asterinas-osdk-bin.elf \
  --initramfs target/debian-x86/stage1/initramfs.cpio \
  --root-image target/debian-x86/m6/rootfs/debian-root.ext2 \
  --output target/x86-debian/boot.transcript \
  --wait-for "Reached target multi-user.target"
```

`make test_x86_debian_boot` runs the same thing from the Makefile.

Add `--accel kvm` for latency measurements. `--debugcon PATH` captures writes to
QEMU's port 0xe9, and `--serial-log PATH` captures `early_print` output — the
panic stack trace goes there, not to the hvc0 console.

### The LMbench regression entry

`make test_x86_debian_lmbench` boots the prepared LMbench image and waits for
`LMBENCH_RUN_DONE`. It echoes the image, CPU count, accelerator, loglevel and
QEMU wrapper, so a transcript can be attributed to a configuration:

```sh
# Host: pin the vCPUs and stop frequency scaling from moving the measurements.
sudo cpupower frequency-set -g performance        # or write scaling_governor
LMBENCH_QEMU_CPUS=0,2,4,6 make test_x86_debian_lmbench \
  X86_DEBIAN_KERNEL=target/osdk/asterinas-osdk-bin.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_LMBENCH_IMAGE=target/x86-debian/m4/lmbench2.ext2 \
  X86_DEBIAN_LMBENCH_QEMU=tools/x86/debian/lmbench_qemu_pin.sh \
  X86_DEBIAN_SMP=4
```

`tools/x86/debian/lmbench_qemu_pin.sh` is a plain `taskset` wrapper; the CPU
list is site specific (on the i5-1340P used here, CPUs 0,2,4,6 are one thread
of each P-core). The image is built once with
`tools/x86/debian/prepare_lmbench_disk.sh` from a published rootfs and the
runtime archive produced by `tools/riscv/lmbench_native.py package`.
