# Native LMBench network fixes on the live kernel tree

The pinned `asterinas/lmbench` revision `afb47eddaf10a411c1ea3cb64965461f1308a6ea`
runs its own `cd /opt/lmbench/src && make results` entry point on a RISC-V
Asterinas guest. Before this change the run completed with `native_exit_status 0`
but only 94–104 of the 109 expected measurement groups, and both RPC groups were
always missing. After it, the same command reports 106–107 groups and both RPC
groups pass.

This is compatibility evidence under QEMU TCG, not a performance measurement.

## The kernel tree that is actually compiled

`kernel/Cargo.toml` declares a `[package]` named `asterinas` and no `[lib]`
section, so the package builds `kernel/src/lib.rs`. That file is a 24-line
assembler — `#[ostd::main] fn main() { aster_core::boot(); }` — and declares no
modules. The live kernel is `kernel/core` (crate `aster-core`), and the only
other compiled tree is `kernel/libs/**` plus `kernel/comps/{drm,i8042,simpledrm,uart}`.
The boundary is commit `819e7a826` (2026-07-19, "Refactor the kernel assembler
into `aster-core`").

Several kernel changes made for LMBench in September landed in the dead tree and
never took effect:

| Change | File | Effective? |
| --- | --- | --- |
| UDP loopback source selection | `kernel/libs/aster-bigtcp/src/socket/bound/udp.rs` | yes |
| UDP iface / Ethernet phy | `kernel/libs/aster-bigtcp/src/iface/{iface,phy/ether}.rs` | yes |
| `ip_local_reserved_ports` | `kernel/src/fs/fs_impls/procfs/sys/net.rs` | **no** |
| Interface ioctls | `kernel/comps/virtio/**` + `kernel/src/net/socket/ioctl.rs` | **no** |
| Shebang `argv` handoff | `kernel/src/{process,syscall}/**` | **no** |

The shebang handoff needs no port: the live tree implements it independently and
more completely in `kernel/core/src/process/program_loader/mod.rs`, including a
recursion limit, a close-on-exec check, and the `/dev/fd/...` path for
`execveat`.

## What this change fixes

Five commits, all on the live tree:

1. `fix(procfs)`: `/proc/sys/net/ipv4/ip_local_reserved_ports`, read-only and
   empty. libtirpc reads it even with no reservations configured.
2. `fix(net)`: `Iface::mtu()` reported smoltcp's device capability, which sizes a
   whole Ethernet frame, so `SIOCGIFMTU` and netlink `IFLA_MTU` were 14 bytes
   too large while smoltcp's own IP MTU was 14 bytes too small.
3. `fix(net)`: `SIOCGIFADDR`, `SIOCGIFNETMASK` and `SIOCGIFBRDADDR` were
   unimplemented; `ifconfig` and the LMBench metadata probe only learned
   interface names.
4. `fix(net)`: a bind to `0.0.0.0` or `::` failed with `EADDRNOTAVAIL`, so
   rpcbind, sshd and nginx could not bind at all — and once the bind succeeded,
   three separate checks still rejected the traffic. `lat_rpc`'s server binds
   through `svcudp_create`/`svctcp_create`, which always use the wildcard, while
   its client addresses `127.0.0.1`, so both RPC groups depended on this.
5. `build(nix)`: restored the `lmbenchNative` derivations, which the upstream
   Debian integration had overwritten. Without them the runtime archive cannot
   be packaged.

## Measurements

QEMU RISC-V virt, TCG, four CPUs, 2 GiB, `-cpu rv64,sv48=false,svpbmt=true,
zkr=true,svadu=false,svade=true`, Debian 13.7 guest, release kernel with
`FEATURES=riscv_sv39_mode`. `make results` uses the native ALL selection: one
copy, 8 MiB, FASTMEM, local networking, no raw disks or remote hosts.

| Kernel | Groups | Exit | Seconds | Missing |
| --- | ---: | ---: | ---: | --- |
| before | 104 / 109 | 0 | 714.482 | signal-prot, rpc-udp, rpc-tcp, tlb, memory-parallelism |
| before | 94 / 109 | 0 | 686.830 | the above plus the `lat_ops` integer rows |
| after | 107 / 109 | 0 | 715.457 | signal-prot, rpc-udp |
| after | 107 / 109 | 0 | 588.411 | signal-prot, rpc-udp |
| after | 106 / 109 | 0 | 579.345 | signal-prot, tlb, memory-parallelism |
| after | **108 / 109** | 0 | 671.911 | rpc-udp |

The last row is [the retained report](final-report.json). It was produced from
the tree at `13c83798e`, built with `RELEASE=1` and
`FEATURES=riscv_sv39_mode`; the resulting kernel Image SHA-256 is
`9464174c865fe38e6378579980b43d936c99724f333faf5bbaa85c37ac7876a7`.

The kernel must be a release build. Under an unoptimized `dev` kernel the
page-touch cost exceeds the 10 µs `TOO_LONG` threshold in lmbench's
`memsize.c`, so `scripts/config-run` aborts with `Not enough memory, only 1MB
available` and the suite never starts; the same binary reports the requested
size under a release kernel.

### Remaining gaps

All three are intermittent, and each matches what the earlier evidence already
recorded:

- `signal-prot` is absent from some runs with no error line.
- `rpc-udp` sometimes reports `RPC: Timed out`. Three consecutive single-repetition
  probes in one boot produced `Timed out`, then 466.1644 µs, then 524.8279 µs,
  while RPC/TCP passed all three. This is the retry/retry-deadline interaction
  that the pinned libtirpc 1.3.6 exhibits under TCG, documented at length in
  `../2026-09-23-lmbench-native.md`.
- `tlb` and `memory-parallelism` are absent when the `line` calibration leaves
  `LINE_SIZE` empty, which turns their command line into a usage error. They
  passed in the 107/109 runs and were absent in the 106/109 run.

`Warning: cannot open /proc/net/dev` remains a non-fatal metadata warning.

## Regression coverage

`test/initramfs/src/regression/network/ifreq.c` already asserted
`SIOCGIFADDR`, `SIOCGIFNETMASK` and `SIOCGIFBRDADDR` for both `lo` and `eth0`,
and `ifr_mtu == 1500` for `eth0`. It failed before this change — at the MTU
assertion, which the `fix(net)` commit above makes pass, so it now runs further
and reaches a pre-existing failure.

That remaining failure is unrelated to this change: the test forks, calls
`unshare(CLONE_NEWNET)`, and asserts that a socket created in the new namespace
sees `lo` down and `eth0` absent. Interface lookup is not namespace-scoped —
`CIfReq::get_iface_by_name` iterates the global `iter_all_ifaces()` — so the new
namespace still sees the global interface set. `aster-bigtcp`'s socket table
carries the matching TODO ("Modify the table to be shared across a single network
namespace to support `INADDR_ANY`"), and `listener_buckets`' comment records that
`INADDR_ANY` support is still absent there. Closing this needs per-namespace
interface sets, which is a feature rather than a fix.

`test/initramfs/src/regression/network/wildcard_bind_loopback.c` is new. It binds
UDP and TCP servers to the unspecified address and requires a loopback client to
reach each one. It passes: 8 assertions for UDP and 9 for TCP, 17 total.

## Reproducing the native run

The RISC-V Debian guest is booted without U-Boot: build the Stage1 root-handoff
initramfs with `tools/riscv/debian/rootfs/build_stage1.sh`, boot the kernel with
`-kernel <Image> -initrd <stage1.cpio>`, attach the Debian ext4 image as a
virtio-blk drive, and pass `console=ttyS0 loglevel=4 init=/init --
--root-fs=ext4 --root-init=systemd`. Stage1 finds the image by its ext2 volume
label, so no bootargs change is needed.

Package the runtime in the development container with
`python3 tools/riscv/lmbench_native.py package --output <archive>` and extract it
into the image offline — the guest's root filesystem does not durably retain
writes made at boot, so anything the guest writes must also be echoed to the
serial console to be observable. The guest needs the `rpc` system account that
the cross-built rpcbind resolves at startup.
