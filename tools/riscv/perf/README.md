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

Before calling the matrix complete, run:

```sh
python3 tools/riscv/perf/audit_baseline.py /path/to/manifest.json
```

The audit exits non-zero until Debian LMBench coverage and a main-branch ext4
baseline are present. Stacked PR measurements remain useful optimization
controls, but cannot satisfy those two release-gate conditions.

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

The fixed-topology LMBench diagnostic attaches the same Debian ext4 rootfs,
but still executes Nix-built binaries from the initramfs, not Debian chroot.
None of the following measurements satisfies the Debian userspace baseline.
The `lat_syscall null` diagnostic measured 5.7806 us on Asterinas
versus 1.2911 us on Linux (4.477x median, 4.521x p95). Raw samples and
provenance are retained in `lmbench-debian-syscall-comparison.json`; process,
filesystem, and network LMBench cases remain required before this section is
considered complete.

The matching `lat_proc fork` case measured 3,110.7 us on Asterinas versus
3,571.0 us on Linux (0.871x median, 1.125x p95); the p95 reversal is driven by
one retained Asterinas outlier. Full provenance is in
`lmbench-debian-process-comparison.json`.

The ext4 diagnostic (`lat_syscall stat`) measured 18.7352 us on
Asterinas versus 10.9409 us on Linux (1.712x median, 1.682x p95). Each sample
uses a fresh rootfs clone; repeated boots of the writable image reproducibly
returned ESTALE after the first successful run. This remains an unresolved
filesystem durability/reopen failure, not a proven fixture-only problem;
details are retained in `lmbench-debian-fs-comparison.json`.

The LMBench TCP loopback case measured 357.92 us on Asterinas versus 230.05 us
on Linux (1.556x median, 1.547x p95). The QEMU virtio-net device is present,
but this case measures guest loopback; host-facing virtio-net remains covered by
the iperf3 throughput/connect benchmark. Results are retained in
`lmbench-debian-net-comparison.json`.

The Debian ext4 control path is now available for Linux: the disposable Debian
13.7 rootfs contains Debian-native `fio` 3.39, `sqlite3` 3.46.1, and `iperf3`
3.18 packages, and the Linux initramfs loads the matching virtio-mmio,
virtio-blk, ext4, jbd2, and CRC32C modules. Four Linux fio cases have completed
one warmup plus five samples with a 256 MiB test file and fixed QEMU settings;
their arrays and raw logs are retained externally. `build_asterinas_initramfs.sh`
and `run_asterinas_fio_sample.sh` provide the matching Asterinas runner. On the
stacked ext4 PR #179 kernel, after journal recovery on a clean copy of the same
rootfs, all four Asterinas cases completed one warmup plus five samples. The
resulting throughput ratios were 0.182x (sequential read), 0.185x (sequential
write), 0.171x (random read), and 0.154x (random write). These are a stacked
ext4 control, not a claim about `main`: the current main-based kernel still
returns `ENODEV` for an ext4 mount. Retain the full comparison JSON and raw
logs before using the numbers for optimization decisions.

The native SQLite ext4 workload is provided by
`debian/sqlite_ext4_case.sh` (with `debian/sqlite_ext4.sh` as the chroot
wrapper). It measures a fixed 10,000-row transaction, full synchronous DELETE
journal, index creation, and count query in microseconds. Its stacked ext4
control result was 1.080x Asterinas/Linux at the median and 1.122x at p95;
the main-based kernel remains excluded until ext4 support lands on main.

The Debian `iperf3` network runner uses guest `10.0.2.15`, QEMU user networking,
and a `virtio-net-device` with the host as the fixed server. The stacked ext4
control reached 209.9 versus 596.1 Mbit/s (Asterinas/Linux **0.352x** median,
**0.361x** p95). A separate fixed one-second transaction around connection
setup measured **1.053x** median and **1.618x** p95. It is reported as
connect-plus-transaction latency because iperf3's one-byte mode exits with a
zero-duration error; raw failure evidence is retained and excluded.

PR #179 also includes a protocol-neutral virtio-net change (`321e6c3f5`) that
reclaims completed TX descriptors before checking queue capacity. It rebuilt
cleanly, but the fixed five-sample rerun measured 210.2 versus 596.1 Mbit/s
(Asterinas/Linux **0.353x** median), statistically unchanged from the previous
209.9 Mbit/s control (**1.002x** of the previous median). The optimization is
therefore retained as a correctness/progress fix, not claimed as a throughput
win; details and raw samples are in `iperf3-network-optimization-pr179.json`.

The common Debian operation controls are now collected on the same stacked
ext4 kernel and fixed QEMU configuration. Each value is the elapsed time for a
fixed loop inside the guest (one warmup plus five samples), so these are
workload-level controls rather than isolated syscall latencies:

| Case | Asterinas median | Linux median | Asterinas/Linux |
| --- | ---: | ---: | ---: |
| 100 fork+exec `/bin/true` | 429,418 us | 503,064 us | **0.854x** |
| 1,000 path `stat` calls | 6,929,490 us | 6,385,665 us | **1.085x** |
| 1,000 descriptor `fstat` calls | 434,910 us | 525,307 us | **0.828x** |
| 50 single-block `fsync` writes | 947,101 us | 576,596 us | **1.643x** |

The fstat case uses Python `os.fstat` because Asterinas does not currently
expose `/proc/self/fd` symlinks. The fsync case includes the fixed `dd`
process/command overhead. Full p95/stddev and raw logs are retained in
`common-ops-comparison-pr179.json` and the corresponding `*-common-*` sample
directories; these results point to fsync/writeback as the next optimization
target.

The SMP concurrency control now has a native Debian `hackbench` case using
`-g 8 -l 1000 -p -T` on four vCPUs. Its median turnaround was 23,721,000 us on
Asterinas versus 9,188,000 us on Linux (ratio **2.582x**); p95 and standard
deviation are retained in `hackbench-comparison-pr179.json`. The schbench
binary uses `-F 256 -n 5 -r 10 -i 20`; its request-latency 99th percentile
median was 70,528 us on Asterinas versus 37,824 us on Linux (ratio **1.865x**).
The host runner extracts this value from the retained QEMU log because the
minimal guest shell cannot reliably parse the multi-line report. Full
p95/stddev are in `schbench-comparison-pr179.json`.

The ext4 dirty-state optimization in PR #179 (`3990e528a`) was rebuilt in
RELEASE mode and rerun with the same fsync workload. Median elapsed time fell
from **947,101 us** to **625,985 us** (0.661x of the previous Asterinas run);
the Linux control is **576,596 us**, so the optimized Asterinas/Linux ratio is
**1.086x** at median and **1.065x** at p95. Full samples and provenance are in
`fsync-optimization-comparison-pr179.json`.

Evidence correction: the historical LMBench JSON files are diagnostic only;
their binaries ran from a Nix-built initramfs rather than a Debian chroot, and
the measured kernel predates the clean metadata fix. PR #179 commit
`43100fe79` now prevents the previously reproducible ext4 ESTALE-on-reopen
failure; formal Debian LMBench runs must be rebuilt after that commit with
exact binary and kernel hashes retained.

The formal runner is available at `debian/lmbench_chroot_case.sh`. It emits
`LMBENCH_EXECUTION_USERSPACE=debian_chroot` and invokes each benchmark through
`chroot /ext2`; `debian/prepare_lmbench_rootfs.sh` installs only binaries whose
interpreter has been patched to Debian's RISC-V loader. Pass the expected
userspace as the final argument to `run_lmbench_sample.sh` so a sample fails
if the benchmark escapes back into the initramfs. A first clean boot
validation of `lat_syscall null` completed on the ext4-fix kernel at 4.8344
microseconds, with the raw log and input hashes retained outside the
repository. `debian/build_lmbench_binaries.sh` can instead compile the four
needed binaries with `riscv64-linux-gnu-gcc` against the Debian RISC-V
sysroot; that path was validated in a clean boot at 4.7409 microseconds. Both
results validate the execution boundary, not the complete repeated comparison
matrix.
