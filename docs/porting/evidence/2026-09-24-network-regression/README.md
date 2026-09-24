# RISC-V network regression on the LMBench integration

The combined network regression was run on the LMBench/ICMP integration
branch with the Linux-compatible TCP fault assertions in `tcp_err.c`:

```sh
tools/docker/run_dev_container.sh --workspace "$PWD" -- \
  make run_kernel AUTO_TEST=regression TARGET_ARCH=riscv64 SMP=4 \
  FEATURES=riscv_sv39_mode RELEASE=1 \
  REGRESSION_TEST_DIRS='[ "network" ]'
```

The [output excerpt](output-excerpt.txt) shows the run reached `tcp_err`:
`test_sendmsg_and_recvmsg` reported 12 passed,
zero failed. The separate `tcp_user_buffer_prefault` regression also passed.
The run then failed in `udp_broadcast`, so **the network suite as a whole did
not pass**. In particular, with `SO_BROADCAST` disabled, `sendto()` and
`connect()` to `127.255.255.255` succeeded where the test expected `EACCES`.
The resulting connected socket also made the subsequent `send()` succeed
where the test expected `EDESTADDRREQ`.

The broadcast mismatch predates this integration: `Iface::broadcast_addr()`
returns `None` for interfaces without the `BROADCAST` flag; the loopback
interface has address `127.0.0.1/8` but only `LOOPBACK | LOWER_UP | UP |
RUNNING` flags. `is_broadcast_endpoint()` therefore omits
`127.255.255.255` from its fixed set. On the Linux host, sending to this
address without `SO_BROADCAST` returned `EACCES` (errno 13), while enabling
the option made the send succeed. This remains an independent kernel issue
for follow-up; no broader network-suite success is inferred from this run.
