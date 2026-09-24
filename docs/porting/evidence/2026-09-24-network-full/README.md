# Complete RISC-V QEMU network regression

The complete `network` regression directory passed on the integrated
LMBench/ICMP source with the TCP fault, loopback broadcast, and regression
harness corrections applied:

```sh
tools/docker/run_dev_container.sh --workspace "$PWD" -- \
  make run_kernel AUTO_TEST=regression TARGET_ARCH=riscv64 SMP=4 \
  FEATURES=riscv_sv39_mode RELEASE=1 \
  REGRESSION_TEST_DIRS='[ "network" ]'
```

The command exited zero. Its [complete compressed output](network-regression.log.gz)
contains one `All regression tests passed.` marker and one
`run_kernel validation passed: mode=regression` marker. All 210 groups that
print a test summary reported zero failures, totaling 1,852 successful
assertions; the output contains no failed assertion lines. Other standalone
checks in the runner, including the UDP/TCP buffer-prefault and dual-stack
scripts, completed before the terminal marker. The Image SHA-256 was
`26835fc6fef49418d83d524a3da25a36b00b09c18352a03feeb914e13ec6be79`.

The original run without a RISC-V virtio NIC reached the end but failed four
`netlink_route` assertions that require `eth0`. `make -n` for the corrected
command confirms `-netdev user,id=regression` and
`-device virtio-net-device,netdev=regression`. The earlier Unix stream failure
came from setting `cmsghdr.cmsg_len` to `CMSG_SPACE(3 * sizeof(int))`, which
included padding as a fourth descriptor. The corrected length uses
`CMSG_LEN(3 * sizeof(int))` while the control buffer and `msg_controllen`
retain `CMSG_SPACE`; `test_scm_rights` then passed 19/19 assertions.

This run validates the suite on RISC-V QEMU with the virtio NIC. It is neither
a physical-board qualification nor a claim that every network behavior or
every standalone network test is covered by this directory.
