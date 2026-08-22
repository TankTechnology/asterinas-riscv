# KE-M6 Report — Minimal Loopback-only Network Namespaces

Track B (kernel evolution), card KE-M6. Commits on `main`:

- `f750661a1` feat(bigtcp): make interface flags changeable at runtime
- `60824056f` feat(net): loopback-only network namespaces via CLONE_NEWNET
- `6ca389966` feat(net): resolve socket bind/connect interfaces in the caller's network namespace
- `391a72d05` feat(net): namespace-filtered netlink route dumps and RTM_NEWLINK flag changes
- `a452f3f72` feat(process): setns(CLONE_NEWNET) and /proc/<pid>/ns/net

This completes the namespace matrix of
`docs/superpowers/specs/2026-08-22-nix-sandbox-namespace-matrix-design.md`:
`CLONE_NEWNET` is the last namespace flag the Nix default build sandbox
requests that the kernel used to reject with EINVAL.

## Scope implemented

- `NetNamespace` (`kernel/src/net/net_ns.rs`): a per-namespace interface
  view plus an owner user namespace. The initial namespace sees the global
  interfaces (loopback + virtio-net); a namespace created with
  `clone(CLONE_NEWNET)` / `unshare(CLONE_NEWNET)` contains only a fresh,
  independent loopback interface — initially **down**, as in Linux — with
  its own background polling thread. Creation requires CAP_SYS_ADMIN in the
  current (possibly newly created) user namespace, matching Linux.
- Socket interface selection (`kernel/src/net/socket/ip/common.rs`) is
  resolved against the *current* namespace's interface view instead of the
  global table: binds to a specific address match only in-namespace
  interfaces, and INADDR_ANY/ephemeral binds pick the namespace's default
  (non-loopback if any, else loopback). A fresh net namespace therefore
  cannot see or touch the host's eth0.
- Netlink route dumps (`RTM_GETLINK` / `RTM_GETADDR`) report only the
  current namespace's interfaces, and `RTM_NEWLINK` requests are now parsed
  (preserving the `ifinfomsg` change mask) and applied to interface flags —
  this is what `ip link set lo up` uses. To support that, bigtcp interface
  flags moved to an atomic word with a `set_flags` accessor.
- `setns(CLONE_NEWNET)` joins a namespace immediately (network namespaces
  are not deferred like PID namespaces) from both nsfs files and pidfds,
  with the usual CAP_SYS_ADMIN checks. `/proc/<pid>/ns/net` exists.
- Incidental fix (caught by the guest test): the privileged-port check
  treated ephemeral port 0 as privileged and always evaluated
  CAP_NET_BIND_SERVICE against the initial user namespace. Port 0 is now
  exempt and the check runs against the current network namespace's owner
  user namespace, as in Linux; without this, no socket could be
  auto-bound after `unshare(CLONE_NEWUSER|CLONE_NEWNET)`.

## Guest verification (`/tmp/kem6/netns_test.c`, riscv64, headless)

`NETNS_TEST_PASS` (21 assertions), with a virtio-net device attached:

- Parent (initial ns) sees `lo` (UP|RUNNING) and `eth0` via RTM_GETLINK.
- Child after `unshare(CLONE_NEWUSER|CLONE_NEWNET)` sees exactly one link
  (`lo`), does not see `eth0`, and `lo` starts with flags
  `LOOPBACK|LOWER_UP` (down).
- `RTM_NEWLINK` brings `lo` up (the `ip link set lo up` path);
  a subsequent dump shows `UP`.
- TCP loopback works inside the new namespace: bind/listen on
  127.0.0.1, a forked connector, accept, and payload delivery.
- The parent's link view is unchanged afterwards.

## Regression evidence

- KE-M4/KE-M5 guest tests re-run on the KE-M6 kernel: `PIDNS_TEST_PASS`,
  `PIDNS_CLONE_TEST_PASS`, `SETNS_TEST_PASS` — all pass.
- LTP process subset (78 entries, SMP=1): `pass=68 fail=4 conf=6`,
  identical to KE-M4/KE-M5 (the four fails are the documented known gaps:
  `clone08`, `clone304`, `tgkill02`, `waitpid01`).
- LTP network subset (44 entries: socket/bind/connect/listen/accept/
  sendto/recvfrom/sendmsg/recvmsg/poll/select/epoll/socketpair/...):
  `pass=30 fail=14 conf=0 crash=0 timeout=1` (epoll01) on the KE-M6
  kernel versus `pass=27 fail=17 crash=3 timeout=1` on the pre-KE-M3
  baseline kernel (`kernel-known-good` @ `9a5034261`) running the same
  initramfs. Every failure on the KE-M6 kernel also fails on the
  baseline; three baseline crashes (`connect01`, `recvfrom01`,
  `sendto01`) are ordinary FAILs now and three more baseline failures
  (`accept01`, `bind01`, `epoll_wait04`) pass now — no new failures.
  Verdict logs: `tools/riscv/pidns/ltp-kem6-net-verdicts.log` and
  `ltp-kem6-net-baseline-verdicts.log`.
- Xfce desktop chain: boots to `graphical.target`, all session milestones
  OK, no kernel panic, desktop screendump unchanged.

## Known gaps (deliberately out of scope)

- No virtual devices beyond loopback: no veth pairs, no bridges; a new
  namespace can never reach the network (which is exactly what the Nix
  sandbox wants).
- Interface flags gate netlink reporting only; traffic is not blocked on a
  down interface (pre-existing bigtcp behavior).
- `/proc/net/dev` and sysfs `/sys/class/net` do not exist yet (the netlink
  route socket is the interface inventory).
- `RTM_NEWADDR`/`RTM_DELADDR`, routing tables, and per-namespace port
  allocation are future work; port allocation is currently per-interface,
  which incidentally gives per-namespace port isolation for free.
- `NS_GET_PID_FROM_PIDNS`-family ioctls remain unimplemented.
