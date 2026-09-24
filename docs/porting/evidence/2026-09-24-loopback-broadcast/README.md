# Loopback directed broadcast regression

The previous [network-suite run](../2026-09-24-network-regression/README.md)
failed because Asterinas allowed `sendto()` and `connect()` to
`127.255.255.255` with `SO_BROADCAST` disabled. A Linux host returned
`EACCES` for that send and succeeded after enabling the option. Asterinas's
loopback interface has `127.0.0.1/8` but lacks `IFF_BROADCAST`, so the
precomputed address set omitted its directed broadcast. This change adds the
loopback CIDR's broadcast address to that set without changing the interface
flags or `SIOCGIFBRDADDR` behavior.

The persistent project container built and ran the RISC-V release kernel with
the same regression command documented in the previous run. The Image SHA-256
was `26835fc6fef49418d83d524a3da25a36b00b09c18352a03feeb914e13ec6be79`.
The [output excerpt](output-excerpt.txt) records all five `udp_broadcast`
groups passing: 6 + 1 + 4 + 3 + 6 assertions, zero failed. `tcp_err` and
`tcp_user_buffer_prefault` also passed. Workspace `cargo fmt --all -- --check`
exited zero.

The **complete network regression did not pass**. It later reached
`unix_stream_err` and received `EPERM` when sending `SCM_RIGHTS` pipe FDs;
subsequent assertions failed, and the test blocked on a pipe read. The
specific QEMU process was stopped, after which `make run_kernel` exited 2
because the terminal success marker was absent. The implicated Unix socket
implementation and test source are byte-identical to remote `main` in this
branch comparison, but this run alone does not establish how remote `main`
behaves at runtime. That issue remains separate from loopback broadcast.
