# Preserve message boundaries in socket `writev`

The generic `writev` loop called `file.write` once per nonempty iovec. On a
connected UDP socket, `writev(["ab", "cd"])` reported four sent bytes but
created two two-byte datagrams. The [Linux oracle](linux-oracle.c) sends one
four-byte datagram. It also confirms that a valid prefix followed by an invalid
iovec returns `EFAULT` without sending a packet, while a missing destination
takes precedence as `EDESTADDRREQ`. Its [output](linux-oracle-output.txt)
retains these three results. The Linux [writev manual](https://man7.org/linux/man-pages/man2/writev.2.html)
describes one atomic transfer across the vectors.

Message-oriented sockets now prefault the complete iovec and call `sendmsg`
once. A disconnected socket's destination error is checked before prefaulting;
empty iovecs still return zero without sending an empty datagram. Stream
sockets retain their prior path because their partial-send behavior needs its
own compatibility investigation. The [QEMU observations](qemu-observations.txt)
record a red run on parent commit `db5e9b892`, a second red run for the
unconnected error priority, and the final green UDP and TCP regressions. The
final kernel image SHA-256 is
`20f55b6af8ca318f4d70f87a0e0f989d8e45a43de61d15e70e4a2873b1086e31`.

The UDP regression uses a nonblocking receiver to check that no extra packet
was emitted. During diagnosis, a blocking UDP socket ignored `MSG_DONTWAIT`
and waited when its queue was empty; that separate receive-side issue is not
covered by this fix. The native LMBench ALL suite passed 109/109 on the parent
branch and was not rerun for this focused change.
