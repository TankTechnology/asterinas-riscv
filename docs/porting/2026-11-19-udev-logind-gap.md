# What keeps udev and logind switched off (2026-11-19)

The desktop image masks two units that a normal Debian boot expects:

```
/etc/systemd/system/systemd-udevd.service  -> /dev/null
/etc/systemd/system/systemd-logind.service -> /dev/null
```

They are masked for a reason, and the reason is not the same for both. This
note records what the kernel already provides, so the decision to unmask is
made against facts instead of symptoms.

## udev

What exists:

* the netlink socket family `NETLINK_KOBJECT_UEVENT` is implemented
  (`kernel/core/src/net/socket/netlink/kobject_uevent/`), and a bound socket
  subscribes to groups the way udev does;
* the device model publishes `/sys/class/{mem,graphics,net,platform}` and
  `/sys/devices/...`, and device nodes are created through `aster_device`
  hooks;
* `/proc/self/mountinfo` reports poll edges, so libmount's monitor works.

What is missing is the *sending* side: `try_send` in `kobject_uevent/bound.rs`
answers `EOPNOTSUPP` ("sending uevent messages to user space is not supported")
and carries a FIXME about delivering to the kernel socket. Nothing in the tree
broadcasts an "add"/"remove" event when a device is registered, so a running
`udevd` would start, subscribe, and then sit on a socket that never speaks.

Conclusion: udev needs a kernel-side emitter on the device-model add/remove
path before unmasking it means anything. The socket, the class devices and the
node creation are already there.

## logind

logind is blocked by something more fundamental: it is built around cgroup v2.
The kernel answers a `clone3` that carries a target cgroup with

```
[    5.524] WARN  : cgroup is not supported      (kernel/core/src/syscall/clone.rs:94)
```

and then ignores the request, so every service logind starts would land in the
root cgroup. Without cgroups there is no per-session accounting, no
`cgroup.kill` for a session teardown, and no way for `systemd-logind` to tell
one seat's processes from another's.

So the ordering is fixed: session management needs the cgroup v2 skeleton
first, and only then is unmasking logind a meaningful experiment. That work is
a feature build, not a configuration change, and it is deliberately not
started here.

## What the mask costs today

* `.mount` units: the desktop image does not rely on udev to mount anything,
  and the mount table notifications already work, so the cost is small.
* No `systemd-logind` sessions: `Xorg` logs `systemd-logind: failed to get
  session` on every start. It falls back to being its own session owner, which
  is why the desktop works anyway.
* No seat/ACL management: `/dev/input` and `/dev/fb0` permissions are handled
  by `desktop-m5-device-access` instead of logind's `uaccess` rules.
