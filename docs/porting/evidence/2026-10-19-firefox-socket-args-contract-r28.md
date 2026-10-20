# Firefox socket syscall argument visibility r28

The host-side probe parser now preserves `arg0..arg3` for socket lifecycle
syscalls 202 (`accept`), 203 (`connect`), 206/207 (`sendto`/`recvfrom`), and
211/212 (`sendmsg`/`recvmsg`), in addition to futex and poll.  This allows the
next probe to bind the final Firefox `recvmsg(212)` wait to its file descriptor
and message pointer without exposing payload data.

Verification: 51 focused tests pass and both probe scripts compile.  This is a
host-code contract; no new guest was run after r27.

