# RISC-V Debian performance baseline

This directory contains the reproducible comparison contract for Asterinas and
Linux. It is intentionally separate from the existing x86 benchmark publisher:
the RISC-V baseline must keep the guest, QEMU, rootfs, and kernel provenance
alongside every measurement.

## Measurement contract

Run both systems with the same QEMU machine, CPU model, memory, SMP count,
virtio block/network topology, Debian userspace, and benchmark data. Build the
Asterinas kernel with `RELEASE=1`; record the Linux kernel and mitigation state
explicitly rather than assuming they match. Warm up each case, then collect at
least three samples (five is the default for the first baseline). Keep raw
samples and the exact QEMU command line in the retained artifact directory.

The first matrix is:

| Area | Initial cases |
| --- | --- |
| LMBench | syscall/process/fs/network latency |
| filesystem | fio ext4 sequential/random read/write; SQLite ext4 |
| network | iperf3 TCP throughput and connect latency |
| concurrency | hackbench and schbench on SMP |
| Debian hot paths | fork+exec+wait, stat, fstat, fsync |

Use `aggregate_results.py` after each case. Its output reports median, p95, and
population standard deviation for both systems plus
`ratio_asterinas_over_linux` for every statistic. The ratio is always
`Asterinas / Linux`; values above one mean slower latency for latency tests but
higher throughput for bandwidth tests, so the benchmark's `direction` must be
interpreted with its unit.

Example input:

```json
{
  "schema_version": 1,
  "benchmark": "stat-ext4",
  "unit": "us",
  "direction": "lower_is_better",
  "configuration": {
    "target_arch": "riscv64",
    "smp": 4,
    "memory": "2G",
    "release": true,
    "qemu_cpu": "rv64",
    "mitigations": "recorded"
  },
  "samples": {
    "asterinas": [18.1, 17.9, 18.4, 18.0, 18.2],
    "linux": [9.6, 9.8, 9.7, 9.6, 9.7]
  }
}
```

```sh
python3 tools/riscv/perf/aggregate_results.py input.json \
  --output result.json
```

The existing native LMBench workflow in `tools/riscv/lmbench_native.py` is the
preferred first runtime source. Its compatibility smoke is a gate, not a
performance number; use the native `make results` run for the baseline. The
existing `bench_linux_and_aster.sh` remains useful for the suites that already
provide a Linux control, but its x86-oriented defaults must not be reused for a
RISC-V claim without recording the changed machine configuration.
