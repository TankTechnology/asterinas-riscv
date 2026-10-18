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
uses the same path.

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


## Running the native LMbench ALL suite

`python3 tools/riscv/lmbench_native.py package --architecture amd64` builds the
runtime archive; `tools/x86/debian/prepare_lmbench_disk.sh` copies a published
rootfs and installs it, so the guest only runs the benchmark. The Kbuild results
come from the upstream driver's own `make results`, reached through
`asterinas-native.py`.

Observed on this kernel, 4 vCPU, `-cpu Icelake-Server`:

```
LMBENCH_RUN_NATIVE_EXIT 2 <ts>      make exit 2 (the coverage gate, not a crash)
  "native_exit_status": 0,          the inner make itself succeeded
  "timed_out": false,
  "elapsed_seconds": 2235.5,
  observed: 105 of 109
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

Measured before and after, same disk and command: 0 of 109 at 34 s, then 105 of
109 at 2235 s. The runner's deadline also had to be raised — its 900 s default
cut the run off at 905 s having reached 73 of 109 — so the unit sets
`LMBENCH_TIMEOUT=3600`.

### What is still missing

Two measurements: `rpc-udp` and `rpc-tcp`. `signal-prot` was measured on the
latest run, so it is no longer among them.

`passed` is therefore still false, and the gate was not loosened to change it.

### Why the RPC measurements fail

A plausible cause took three probes to pin down, and two of them were dead ends
worth recording so they are not repeated.

**Discard these two.** An early probe asked the portmapper for `XACT_PROG`
version 2 and got port 0 for both transports, which looked like the server
never registering; LMbench registers version 1, so that probe always returns 0
and says nothing. A later probe appeared to show that a bare `lat_rpc -s` fails
where `-s 127.0.0.1` succeeds; the source settles it —
`opt/lmbench/src/lat_rpc.c` handles `-s` by forking, running `server_main()` in
the child, and having the parent `exit(0)` from inside `getopt`, so nothing
after `-s` is ever read. The two forms are identical and that single success
was an outlier.

**What the numbers say.** Eighteen trials of the client/server pair in one
boot, each with the server freshly registered and the portmapper verified
first: **18 of 18 failed**, each after 39-58 s, with an empty server log
meaning registration had succeeded. That is not variance. Against it, the same
full run measured:

```
UDP latency using localhost   9941.5575 microseconds
TCP latency using localhost  11125.6734 microseconds
```

libtirpc's default `CLSET_RETRY_TIMEOUT` is 2.5 ms. A loopback round trip that
takes 9.9 ms cannot answer inside a 2.5 ms retry window, so every call's first
attempt expires, the client retransmits, and the call eventually fails. That
also explains the one intervention the 2026-09-23 work found that made the
measurement complete: a diagnostic build raising the retry timeout to 25 ms,
which is the first value above 9.9 ms. That work declined to ship it, correctly
— it modifies the benchmark rather than the kernel.

**What is not settled.** Whether ~10 ms of loopback latency is simply this
kernel under TCG, or whether the poll scheduler adds a fixed cost on top of it.
1 ms is the scheduler's unit (`next_poll_at_ms`), which bounds it from below
but does not explain 10 ms; a native machine would measure tens of
microseconds here. Until that is answered, `rpc-udp` and `rpc-tcp` should be
expected to stay missing, and the honest options are to find and remove the
added latency, or to accept the gap and say so. Changing the benchmark is not
one of them.

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
