# NIXOS-N1 — AF_NETLINK subsystem completion (KOBJECT_UEVENT + ROUTE dump)

Date: 2026-08-23
Branch: `track/nixos`
Commits:
- `ed41a4875` feat(kernel): netlink socket options, RTM_SETLINK/RTM_NEWADDR, pktinfo control message
- `3e1d36387` test(nixos): NIXOS-N1 netlink guest gate (nlprobe + busybox ip)
- (boot-driver fix + this report)
Status: **Complete — all guest gates green; systemd-udevd no longer reports "Protocol not available".**

---

## 1. Starting state

A previous session (cut off mid-work) left ~300 lines of uncommitted changes
implementing most of the netlink option surface. This session took them over,
fixed two real bugs in them, and completed verification:

- `NetlinkSocket::get_option` had duplicate, dead `PktInfo`/`ExtAck` match arms
  (the second copy read via `.get().unwrap()` and wrote socket state from a
  *getter*). Removed the dead arms; `NETLINK_PKTINFO` get now returns the
  stored socket-level state instead of a hardcoded `false`.
- `ctrl_msg.rs` carried an unfulfilled `#[expect(non_camel_case_types)]`;
  replaced with the actually-triggered `clippy::upper_case_acronyms`.

The rest of the inherited work was sound and is kept as-is:

- `NETLINK_PKTINFO` (get/set; `recvmsg` attaches an `nl_pktinfo` control
  message, new `ControlMessage::Netlink` variant),
  `NETLINK_EXT_ACK`, `NETLINK_GET_STRICT_CHK`, and
  `NETLINK_LIST_MEMBERSHIPS` (get-only, 1-based group IDs like Linux, supports
  the NULL-buffer size query used by `sd_netlink_open`).
- `SO_DOMAIN` / `SO_PROTOCOL` for netlink sockets.
- `getsockopt`: a NULL `optval` with zero `optlen` is now legal (size query).
- rtnetlink: `RTM_SETLINK` accepts no-op flag changes (e.g. systemd setting
  IFF_UP on `lo`), `RTM_NEWADDR` reports `EEXIST` for an address the interface
  already has (`EOPNOTSUPP` otherwise — `Iface` is still read-only), and both
  return an empty `NLMSG_ERROR` ACK when `NLM_F_ACK` is set. `IFA_ADDRESS` /
  `IFA_LOCAL` attributes are now parsed instead of skipped.

## 2. What was already there (upstream skeleton)

`socket(AF_NETLINK, …)` for `NETLINK_ROUTE` and `NETLINK_KOBJECT_UEVENT`,
bind with automatic port assignment, unicast/multicast tables, and the
`RTM_GETLINK`/`RTM_GETADDR` dump paths existed already. The reason systemd
still logged "Failed to open netlink, ignoring: Protocol not available"
(ENOPROTOOPT) was the missing option surface above, not socket creation.

## 3. Guest verification

New gate `tools/riscv/nixos/n1/` (probe `nlprobe.c`, initramfs `init_n1.c`,
`build_n1.sh`, `boot_n1_smoke.py`). Boots QEMU from a **private** disk at
`/tmp/n1-netlink/boot.ext4` with a `virtio-net` device; the shared
`target/qemu-uboot/current/boot.ext4` is never touched. Boot with
`loglevel=warn` so the syscall-trace noise cannot split marker lines.

Probe + BusyBox `ip` results (9/9 checks OK):

```
uevent: membership size query -> optlen=4
uevent: memberships: 1
__NL_UEVENT_OK__
link: index=1 name=lo flags=0x10049
link: index=2 name=eth0 flags=0x11043
getlink: 2 links (lo=1 eth0=1)
__NL_GETLINK_OK__
addr: family=2 index=1 prefix=8 addr=127.0.0.1
addr: family=2 index=2 prefix=24 addr=10.0.2.15
addr: family=10 index=1 prefix=128 addr=0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.1
__NL_GETADDR_OK__
newaddr: ACK -EEXIST as expected
__NL_NEWADDR_OK__
```

BusyBox `ip` (note: `CONFIG_IP` alone is not enough — the `IPLINK`/`IPADDR`
subcommand applets must be enabled too; fixed in `build_busybox.sh`):

```
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65535 ...
    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
    inet 127.0.0.1/8 scope host lo
    inet6 ::1/128 scope host
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1514 ...
    link/ether 52:54:00:12:34:56 brd ff:ff:ff:ff:ff:ff
    inet 10.0.2.15/24 brd 10.0.2.255 scope global eth0
```

Full systemd guest (`systemd-initramfs.cpio.gz` + this kernel, private disk
`/tmp/n1-systemd/boot.ext4` via the new `--boot-disk` option of
`boot_systemd_smoke.py`):

- Before (M-era log, `target/nixos/systemd/systemd-serial.log.smp1`):
  `Failed to open netlink, ignoring: Protocol not available` (1 occurrence,
  logged by systemd PID 1 right after `Hostname set to ...`).
- After: **0 occurrences** of both `Failed to open netlink` and
  `Protocol not available`; systemd reaches `Multi-User System` and the login
  prompt (`Startup finished in 5.591s (kernel) + 3.328s (userspace)`).

## 4. Known gaps (out of N1 scope)

- **Socket ioctls** (`SIOCGIFMTU`/`SIOCGIFTXQLEN`/…): not implemented; BusyBox
  `ip` prints a harmless `ip: ioctl 0x8942 failed` line per interface but still
  reports correct data from rtnetlink.
- **uevent broadcast**: `UeventMessage` and the multicast group machinery
  exist, but nothing in the kernel emits uevents yet (no device-event source
  is wired up). udevd copes: it cold-plugs via `/sys` scanning and only misses
  hotplug events.
- `Iface` is read-only, so real `RTM_NEWADDR`/`RTM_SETLINK` mutations return
  `EOPNOTSUPP` (no-op requests succeed). `RTM_NEWROUTE`/`RTM_GETROUTE` are not
  implemented (no routing table in the kernel yet).
- `NETLINK_EXT_ACK` is accepted but no extended-ACK TLVs are ever emitted;
  strict checking (`NETLINK_GET_STRICT_CHK`) always reads as disabled.

## 5. Incidents / notes

- First systemd re-verification accidentally booted the **shared** disk
  because `boot_systemd_smoke.py`'s QEMU argv used the module-level
  `BOOT_DISK` constant while only the existence check honored the new
  `--boot-disk` argument. Fixed; the shared disk was only read (U-Boot
  `ext4load`, no mounts), not written.
- Tooling path gotcha: scripts under `tools/riscv/nixos/n1/` are one directory
  level deeper, so the repo-root relative computation needs one more `..`
  than the `m*` siblings at `tools/riscv/nixos/`.
