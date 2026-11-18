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
logind, so the probe names one of the holes: `SO_TIMESTAMPNS` is not
implemented. The kernel already has `SO_TIMESTAMP` (29) recorded in
`net/socket/util/options.rs`, which is why journald works; the nanosecond
variant (35) is missing from the option set. The asymmetry is worth naming:
the *data* plane already knows 35 -- `SCM_TIMESTAMPNS` is defined in
`net/socket/util/message_header.rs` for attaching a timespec to received
messages -- while the *control* plane never registers it as a settable
option. The number is implemented; its entrance is not.

Accepting 35 (record the request, like 29) was necessary but **not
sufficient**: logind still died with the same message. The next step was to
make the kernel name the option. Three `debug!` lines in `setsockopt(2)` and
`getsockopt(2)` print the level and option number on every rejected lookup,
and the boot that followed carried:

```
setsockopt: level=SOL_SOCKET optname=26 is not supported (ENOPROTOOPT)
setsockopt: level=SOL_SOCKET optname=34 ... 76 ... (probes, harmless)
getsockopt: level=SOL_SOCKET optname=31 ... 77 ... (probes, harmless)
setsockopt: level=SOL_SOCKET optname=32/33 rejected (EPERM; sd-device expects
          the *BUFFORCE fallback)
```

26 is `SO_ATTACH_FILTER`: sd-device installs a classic-BPF filter on the
monitor so the kernel delivers only matching subsystems. `ATTACH_FILTER` was
in the option-name enum but had no arm in `new_socket_option`, so it fell to
the catch-all `ENOPROTOOPT`. `DETACH_FILTER` (27) had the same hole from the
other side: a handler arm existed, but no way to construct the option.

Both are wired now. `SO_ATTACH_FILTER` reads its `struct sock_fprog` through
the existing `read_prog_from_user` (which validates jump targets), and the
filter lands in `BoundNetlink::filter`, where the receive path applies it. A
filter attached **before** `bind` used to be silently dropped, which would
have turned the option into "accepted but ignored" -- exactly the failure
this note keeps finding; it is now carried across `bind` the way the eBPF
filter already was.

With both options, logind starts in QEMU (2026-11-19):

```
systemd-logind[408]: System has /dev/tty0 but not /sys/class/tty/tty0/active
                     which is broken, ignoring: No such file or directory
systemd-logind[408]: New seat seat0.
systemd[1]: Started systemd-logind.service - User Login Management.
$ systemctl is-active systemd-logind
active
```

`loginctl list-sessions` answers with a header and no sessions: the desktop
session is a systemd service, not a PAM login, so nothing has created one yet.
That is the integration work, not a blocker.

`SO_TIMESTAMPING` (38) and `SOL_NETLINK` `PKTINFO`/`EXT_ACK` are still
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

1. **The two socket options** (done 2026-11-19): `SO_TIMESTAMPNS` accepted
   and recorded, `SO_ATTACH_FILTER`/`SO_DETACH_FILTER` accepted, with the
   classic filter installed on the netlink socket and carried across `bind`.
   Proof, all in QEMU: `setsockopt(SOL_SOCKET, 35)` returns 0 and
   `getsockopt` round-trips 1; `systemctl start systemd-logind` reports
   `active`; the journal shows `New seat seat0.`; and
   `Failed to create udev watchers` is gone. Still open on this step: no
   session is created yet, because the desktop is a service rather than a
   PAM login.
2. **uevent emitter** (done 2026-11-19): a device that is added, removed, or
   asked for an event through its `uevent` file is announced on the kernel
   group. What the pieces were, and what was missing:

   * `kobject_uevent/message/uevent.rs` already built the NUL-separated
     payload with a monotonic `SEQNUM`;
   * `UeventMessage` already implemented `MulticastMessage`, and
     `SupportedNetlinkProtocol::multicast` already delivered to bound
     sockets -- as `dead_code`;
   * `syn_uevent.rs` already parsed what user space writes to
     `/sys/.../uevent`.

   Missing were the `/sys/.../uevent` attribute itself (it did not exist, so
   udevd's coldplug had nothing to open), a publish point at registration,
   and the bridge between `aster-device` and the kernel's netlink code.
   `hooks::KernelHooks` carries it, the way it already carries devnode
   requests. `add` is emitted at the end of registration -- after the sysfs
   directory and `/dev` node exist -- and `remove` before teardown deletes
   them, so a listener that reads the device on the event still finds it.

   Verified in QEMU, 2026-11-19, image built 01:58 (sha256 aa3ea94887de...).
   A `NETLINK_KOBJECT_UEVENT` socket bound to group 1 receives, for
   `echo add > /sys/class/graphics/fb0/uevent`:

   ```
   add@/devices/virtual/platform/framebuffer/fb0\0ACTION=add\0
   DEVPATH=/devices/virtual/platform/framebuffer/fb0\0SUBSYSTEM=graphics\0
   MAJOR=29\0MINOR=0\0DEVNAME=fb0\0SEQNUM=19\0
   ```

   `echo change >` produces `ACTION=change`, and the DEVPATH it carries
   exists under /sys.

   Still open on this step:
   a. ~~unmask `systemd-udevd` and require it to process the coldplug~~ --
      done: `/run/udev/data/c29:0` appears for the framebuffer after a
      trigger, and `udevadm monitor` prints the add and change events;
   b. deliver `SCM_CREDENTIALS`. The netlink receive path has no control
      messages at all -- `recvmsg` carries a `// TODO: Receive control
      message` and returns an empty vector -- and libudev's kernel monitor
      checks that the sender is the kernel (pid 0) before it accepts a frame.
      This one piece explains three observations made the same night:

      * our own python listener receives `add@...` frames;
      * `udevadm monitor --kernel` prints its banner and then nothing;
      * `systemd-udevd` starts and stays `active`, `udevadm trigger` returns
        0, and `/run/udev/data` is still empty.

      `SCM_CREDENTIALS` is now delivered, per socket and only when
      `SO_PASSCRED` was set, carrying the kernel's identity (pid 0). An
      earlier attempt was reverted after a probe reported the ancillary data
      as `(0, 0)` -- that probe was reading `recvmsg()`'s fourth element (the
      netlink address) instead of its second (the ancillary data). With the
      probe fixed the control message is `[(1, 2, b'\0' * 12)]`: SOL_SOCKET,
      SCM_CREDENTIALS, pid=uid=gid=0, which is what libudev checks.

      And udev still ignores the frames: `udevadm monitor --kernel` prints
      nothing and `/run/udev/data` stays empty while the daemon is `active`
      and `udevadm trigger` returns 0. With credentials ruled out, the
      suspects left are how a listener is woken and served by this netlink
      socket (a `udevadm monitor` waits in `epoll` and reads only when
      signalled), and whether it accepts a message that carries no netlink
      header. A blocking `recvmsg` from a plain socket does receive the frame
      and its credentials, so the delivery itself works;
      The experiment that separates them, and the first thing to run next:

      * a minimal client that binds group 1, registers the socket with
        `epoll`/`EPOLLIN` and waits. If it is never woken, the kernel's poll
        path is at fault and libudev is not involved at all;
      * if so, check that `BoundNetlink`'s `Pollable` implementation registers
        the same pollee that `MessageReceiver::enqueue_message` notifies.
        A different pollee would produce exactly this: a blocking `recvmsg`
        returns because the queue is checked, and an `epoll` waiter sleeps
        forever because nothing signals it.

      Both were run. The minimal epoll client is woken (`POLL= [(3, 1)]`) and
      reads the frame, so the kernel's poll path was never the problem. What
      was: the message's source address was the unspecified one, with
      `nl_groups == 0`, and a listener reads that as a unicast frame from
      nobody and ignores it. Stamping the announced group on the source
      address fixed it:

      ```
      KERNEL[25.825497] add      /devices/.../framebuffer/fb0 (graphics)
      KERNEL[28.719694] change   /devices/.../framebuffer/fb0 (graphics)
      $ ls /run/udev/data
      c29:0
      ```

      `systemd-udevd` now processes the device, and the classic-BPF filter
      accepted in step 1 is finally observable: a monitor matching
      `graphics` prints the event above, one matching `block` prints nothing
      for the same write.
   c. ~~only then can the filter attached in step 1 be shown to select~~ --
      done: `--subsystem-match=graphics` receives the event,
      `--subsystem-match=block` does not.

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

Step 1 unblocks logind. Step 2 unblocks udev, and is also what makes the
filter attached in step 1 observable: until the kernel broadcasts uevents
there are no messages for a filter to select. Steps 3-5 are the accounting
story and are independent of the desktop.
