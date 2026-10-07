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

Use `collect_samples.py --output /artifacts/case/system -- command ...` to
retain every invocation's stdout, stderr, exit status and duration. The command
must emit exactly one `PERF_SAMPLE=<number>` line containing the measured guest
metric, not boot/build/wrapper wall time. The default is one discarded warmup
and five measured runs; non-zero exit, timeout, or invalid markers abort the
collection without publishing `samples.json`. Existing output directories are
never overwritten. Copy the resulting numeric arrays into the aggregation
input only after verifying the matching system provenance.

The timeout terminates local child processes only. A Docker/SSH wrapper must
also enforce its own in-container/remote deadline and cleanup; killing the
client does not guarantee that its guest QEMU has stopped. Before another run,
verify that no prior guest remains. This collector does not replace the input
manifest, kernel/rootfs retention, or fixed-configuration checks.

The reported p95 across repeated run values describes run-to-run variation.
It is not per-operation tail latency; retain fio/schbench latency distributions
separately when reporting those tails.

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

## RISC-V Linux control progress

The Linux control uses Debian's official `linux-image-6.12.94+deb13-riscv64`
payload (Linux 6.12.94, `riscv64`) and the same benchmark binaries copied into
the Asterinas initramfs. `build_linux_initramfs.sh` repacks that userspace with
a Linux `/init`; `run_linux_lmbench_sample.sh` boots the guest and emits a
single `PERF_SAMPLE` marker for `collect_samples.py`.

The initial diagnostic control cases use `virt`, `rv64,svpbmt=true,zkr=true`, 8G,
SMP4, and `mitigations=off`. They intentionally do not attach a block device:
these syscall/process cases do not need one, while the Debian kernel package's
virtio block driver is not built into this minimal initramfs. Block-backed
ext4, network, and SMP cases must use a Linux initramfs/rootfs with the matching
virtio drivers before they are compared.

Diagnostic results currently include (not the formal Debian baseline):

| Case | Asterinas median | Linux median | Asterinas/Linux |
| --- | ---: | ---: | ---: |
| LMBench simple syscall | 5.5113 us | 1.2079 us | 4.56x |
| LMBench fork+exit | 3201.2 us | 2971.6667 us | 1.08x |

These are latency ratios (lower is better); the p95 and population standard
deviation remain in the external artifact directory alongside every raw run.
The existing Asterinas samples attach three block devices whereas the Linux
diagnostic samples attach none. Both use Nix-built benchmark initramfs binaries,
not the required Debian userspace. Recollect both systems with identical device
topology and Debian userspace before publishing baseline or optimization claims.
