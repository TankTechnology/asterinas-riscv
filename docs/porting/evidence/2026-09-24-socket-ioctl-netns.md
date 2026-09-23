# Socket interface ioctls retain their network namespace

Linux selects the socket's network namespace for interface ioctls, even after
the calling process changes namespaces. Its
[`sock_ioctl` path](https://raw.githubusercontent.com/torvalds/linux/master/net/socket.c)
obtains the namespace from the socket (`sock_net(sk)`). Asterinas previously looked up the
current thread's namespace for every `SIOCGIF*` and `SIOCGIFCONF` request.

The focused `ifreq` regression now creates an IPv4 datagram socket before
`unshare(CLONE_NEWNET)` and another afterwards. The old socket must still see
the initial namespace's up loopback and virtio interface; the new socket must
see only the new namespace's down loopback. The old socket also checks
`SIOCGIFCONF`. On the previous kernel, the new assertion failed at
`ifreq.c:99` because the old socket saw a down loopback. With this change,
`AUTO_TEST=ifreq` passed on four-CPU RISC-V QEMU:

```sh
make run_kernel AUTO_TEST=ifreq TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1
```

The separate `AUTO_TEST=ifconf` QEMU gate passed all 10 cases with the same
kernel change. Both commands returned zero. The shared socket `FileLike::ioctl`
path now passes the namespace captured at socket creation. Accepted stream
sockets inherit their listener's namespace, including TCP, Unix stream, and
vsock. This changes interface-query behavior only; other socket operations
that still consult the current thread's namespace require separate work.

An x86 kernel build was started but not completed: the shared development
volume reached its non-root free-space limit during compilation. The partial
x86 target artifacts from this worktree were removed. CI remains the x86
build check for this change.
