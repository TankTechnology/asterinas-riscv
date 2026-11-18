# What keeps udev and logind switched off (2026-11-19)

The desktop image masks two units that a normal Debian boot expects:

```
/etc/systemd/system/systemd-udevd.service  -> /dev/null
/etc/systemd/system/systemd-logind.service -> /dev/null
```

They are masked for a reason, and the reason turned out not to be the one this
note originally recorded. It is also not the same for both.

## logind: a missing socket option, not cgroups

The first version of this note said logind was blocked by the absence of
cgroup v2, on the strength of the `cgroup is not supported` warning that
`clone3` prints at boot. Booting the image and starting the unit by hand
shows something else (QEMU, 2026-11-19):

```
systemctl unmask systemd-logind
systemctl start systemd-logind
...
systemd-logind[1212]: Failed to create udev watchers: Protocol not available
systemd-logind[1212]: Failed to fully start up daemon: Protocol not available
systemd[1]: systemd-logind.service: Main process exited, code=exited, status=1/FAILURE
```

`Protocol not available` is `ENOPROTOOPT` (92), which systemd reports as
`Error: code: 92` in `systemctl status`. It comes from `setsockopt`, not from
`clone3`: the cgroup field of `clone3` is dropped with a warning and the
service keeps running, so cgroups cannot be what kills logind.

Which option: a probe inside the guest creates a `NETLINK_KOBJECT_UEVENT`
socket (`AF_NETLINK`, protocol 15) exactly as sd-device does and tries each
candidate option. `ok` means the kernel accepted it:

| level | option | result |
|---|---|---|
| `SOL_SOCKET` (1) | `SO_PASSCRED` (16) | ok |
| `SOL_SOCKET` (1) | `SO_TIMESTAMPNS` (35) | **errno 92** |
| `SOL_SOCKET` (1) | `SO_RCVBUFFORCE` (33) | ok |
| `SOL_SOCKET` (1) | `SO_TIMESTAMPING` (38) | **errno 92** |
| `SOL_NETLINK` (270) | `ADD_MEMBERSHIP` (1), `DROP_MEMBERSHIP` (2) | ok |
| `SOL_NETLINK` (270) | `PKTINFO` (3), `EXT_ACK` (11) | **errno 92** |
| `SOL_NETLINK` (270) | `GET_STRICT_CHK` (12) | ok |

sd-device enables `SO_TIMESTAMPNS` on the monitor socket it creates for
logind, so the probe names the failure exactly: `SO_TIMESTAMPNS` is not
implemented. The kernel already has `SO_TIMESTAMP` (29) recorded in
`net/socket/util/options.rs`, which is why journald works; the nanosecond
variant (35) is missing from the option set.

The asymmetry is worth naming: the *data* plane already knows 35 --
`SCM_TIMESTAMPNS` is defined in `net/socket/util/message_header.rs` for
attaching a timespec to received messages -- while the *control* plane never
registers it as a settable option. The number is implemented; its entrance
is not.

**So the cheapest unblock for logind is a socket option, not a cgroup
subsystem.** Accepting `SO_TIMESTAMPNS` the way `SO_TIMESTAMP` is accepted
(record the request; annotating received messages with `SCM_TIMESTAMPNS` can
follow) is a small change in `net/socket/util/options.rs` plus the option
enumeration. It should be verified with the probe above, then by starting
logind unmasked and watching for the session the desktop creates.

`SO_TIMESTAMPING` (38) and `SOL_NETLINK` `PKTINFO`/`EXT_ACK` are also
unimplemented; nothing observed so far needs them, but they will be the next
`Protocol not available` in some other program.

## logind: what cgroups would still buy

`clone3` carries a `cgroup` field that `syscall/clone.rs` drops with
`warn!("cgroup is not supported")`. The tree already has a cgroup v2 file
system (`fs/fs_impls/cgroupfs`, ~1400 lines) with `cgroup.procs`,
`cgroup.threads`, `cgroup.subtree_control`, `cgroup.events`, `cgroup.freeze`,
`cgroup.max.depth` and controllers for cpu, cpuset, memory (`memory.max`,
`memory.high`, `memory.stat`, `memory.events`) and pids (`pids.current`,
`pids.max`, `pids.peak`); `cgroup.procs` writes already move a running
process, and `cgroupfs::cgroup_node_from_fd` exists precisely to resolve the
`clone3` descriptor. What is missing for `CLONE_INTO_CGROUP` is the wiring in
`TryFrom<Clone3Args> for CloneArgs`, and `cgroup.kill` has no implementation.

None of that blocks logind. It matters for per-unit accounting, for
`cgroup.kill` on scope teardown, and for systemd's own `MemoryMax=` handling.

## udev: still needs an emitter

What exists:

* the netlink socket family `NETLINK_KOBJECT_UEVENT` is implemented
  (`kernel/core/src/net/socket/netlink/kobject_uevent/`), and a bound socket
  subscribes to groups the way udev does;
* the device model publishes `/sys/class/{mem,graphics,net,platform}` and
  `/sys/devices/...`, and device nodes are created through `aster_device`
  hooks;
* `/proc/self/mountinfo` reports poll edges, so libmount's monitor works.

What is missing is the *sending* side: `try_send` in
`kobject_uevent/bound.rs` answers `EOPNOTSUPP` ("sending uevent messages to
user space is not supported") and carries a FIXME about delivering to the
kernel socket. Nothing in the tree broadcasts an "add"/"remove" event when a
device is registered, so a running `udevd` would start, subscribe, and then
sit on a socket that never speaks.

## Order of work, with the command that proves each step

1. **`SO_TIMESTAMPNS`** (small): accept and record it; later annotate
   received messages. Prove with the guest probe above (`errno 92` becomes
   `ok`), then unmask `systemd-logind`, start it, and require
   `systemctl is-active systemd-logind` plus a session entry in
   `loginctl list-sessions` once the desktop has started. Also check that
   Xorg stops logging `systemd-logind: failed to get session`.
2. **uevent emitter** (medium): broadcast `add`/`change`/`remove` from the
   device-model registration path and implement `try_send`. Prove with
   `udevadm monitor` printing the events a device registration produces, then
   unmask `systemd-udevd` and require `udevadm info /sys/class/graphics/fb0`
   to resolve and no unit failures.
3. **`CLONE_INTO_CGROUP`** (small once trusted): plumb the `cgroup` field
   through `CloneArgs` into `cgroupfs::cgroup_node_from_fd` and place the new
   process there at fork time. Prove with `clone3` carrying a cgroup fd (a
   small test program) landing in that cgroup in `/proc/<pid>/cgroup`, and
   with the boot warning gone.
4. **`cgroup.kill`** (small): write `1` to kill every process in the subtree.
   Prove by starting a scope, writing the file, and requiring the processes
   to be gone and the unit to reach `inactive`.
5. **Memory limits** (medium): `memory.max`/`memory.events` already have
   attributes; charging is what needs the work. Prove with a `MemoryMax=`
   service that gets `oom_kill` counted in `memory.events` once it exceeds
   the limit.

Steps 1 and 2 unblock logind and udev. Steps 3-5 are the accounting story
and are independent of the desktop.
