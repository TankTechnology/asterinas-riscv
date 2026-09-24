# Short LMBench replay after baseline build fixes

Integration source `3e827e43ddac08a7b25e3e988a5e5b8cc60b2802` includes
the native LMBench and ICMP stack plus the independent rustfmt fix
`f96be0ee6cac45d432448dea912a46da06483cfa` and cross-architecture
build fix `2a0821ddae1133bed8f4373130242e02fd5fe67b`.
Both merges were conflict-free. The worktree was clean before this evidence
was added.

The persistent project container passed `cargo fmt --all -- --check`,
`make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1`,
and `make kernel TARGET_ARCH=loongarch64`. The RISC-V release Image used
for the replay had SHA-256
`abd5bbc0f62085553a9da630f3b174495bbd6df53f1336d0d08ff94d1f8f2908`.
The native runtime archive separately passed
`python3 tools/riscv/lmbench_native.py verify --archive
target/lmbench-native-20260923/lmbench-runtime-no-egrep.tar.gz`, with
SHA-256 `54bf3c45ca425b8736e54e9723ff50d6c4c306af26ff033c25f89c4272236af3`.

The [QEMU result](qemu-result.json) binds the exact Image and input artifacts
to a passing boot. The [short guest report](smoke-report.json) records
**18/18** passing cases in 8.433 seconds, without case timeouts. The
[identity](identity.json) and [serial transcript](serial.log.gz) include the
guest boot ID, nonce-framed UID-0 responses, and `LIFECYCLE_EXIT status=0`.
The scratch [host driver](../smoke-attempts/scratch-run.py) and its staged
guest script were used without changing the 18 workload arguments.

This short replay uses the older Nix-built smoke archive to cover basic
syscall and memory behavior after the baseline fixes. The native
`cd /opt/lmbench/src && make results` **109/109** qualification on the
pre-fix integrated release Image is recorded in the
[full native result](../../2026-09-24-native-lmbench-integrated-release/README.md).
The baseline fixes do not modify the RISC-V LMBench runner, network stack,
or benchmark binaries, but the full native suite was not rerun on the
post-fix Image. QEMU timings are compatibility evidence, not board or Linux
performance comparisons.
