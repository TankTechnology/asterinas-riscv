# Match Linux stream-send fault boundaries

A short valid iovec followed by an invalid tail previously made `sendmsg`
report a successful short send on AF_UNIX and TCP streams. Socket `writev`
also sent each iovec separately, so it committed the valid prefix before
seeing the bad tail. On the Linux 6.5.0-15 host, these small calls return
`EFAULT` without sending bytes. A much larger valid prefix can still be
reported as a short send when an earlier send-buffer chunk has been committed.
Linux's [TCP copy path](https://github.com/torvalds/linux/blob/v6.5/net/ipv4/tcp.c#L1107-L1151)
commits each copied chunk only after that chunk succeeds.

The [updated regression](../../../../test/initramfs/src/regression/network/tcp_user_buffer_prefault.c)
checks small invalid tails for AF_UNIX `sendmsg` and `writev`, TCP `sendmsg`
and `writev`, and verifies that no bytes escaped before `EFAULT`. It also
checks that a 1 MiB valid prefix followed by a bad tail can make progress on
AF_UNIX and TCP streams. The same test passes on the Linux host.

For stream sends, the prefaulted reader now keeps the deferred fault until the
transport reaches it. The TCP send buffer discards the current contiguous
chunk on a copy fault while preserving previously committed chunks. Socket
`writev` passes all vectors to one `sendmsg` call. The non-socket `writev` path
is unchanged.

The [QEMU observations](qemu-observations.txt) show the updated test failing
on parent commit `d1d199b55`, then passing on the fixed kernel. The adjacent
UDP `writev` and TCP `MSG_DONTWAIT` regressions also passed with kernel image
SHA-256 `e106f84051f053929131f2cf21c1eae23c4c3383c88b52f20b11a833b63d1089`.
The native LMBench ALL suite passed 109/109 on ancestor PR #158 and was not
rerun for this focused fix.
