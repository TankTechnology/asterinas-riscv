# RISC-V LTP syscall gate

This gate runs the Linux Test Project syscall suite on the current Asterinas
RISC-V kernel through U-Boot and QEMU.
The LTP source is pinned to tag `20260529`.
The reviewed repository manifest contains 779 unique enabled names;
the build must package exactly 767 runnable tests and report every unavailable
name in `target/ltp/unavailable-tests.json`.

The gate owns `target/ltp/qemu/` and `target/ltp/results/`.
It never prepares or modifies `target/qemu-uboot/current`,
which remains available for desktop and framebuffer demonstrations.

## Build environment

Run commands from the repository root.
Obtain the pinned test source once:

```bash
git clone --depth 1 --branch 20260529 \
  https://github.com/linux-test-project/ltp.git target/ltp/src
git -C target/ltp/src describe --tags --exact-match
```

The second command must print `20260529`.

Build the Sv39 RISC-V kernel in the project cross-build container:

```bash
docker run --rm --privileged --network=host -v /dev:/dev \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc \
  bash -lc 'restore_owner() { chown -R --reference=/root/asterinas \
      /root/asterinas/target/osdk 2>/dev/null || true; }; \
    trap restore_owner EXIT; \
    export VDSO_LIBRARY_DIR=/root/.local/share/linux_vdso; \
    make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode'
```

Build LTP and package its initramfs in the musl cross-build container:

```bash
docker run --rm --network=host \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas-env:nixos-build \
  bash -lc 'restore_owner() { chown -R --reference=/root/asterinas \
      /root/asterinas/target/ltp 2>/dev/null || true; }; \
    trap restore_owner EXIT; \
    tools/riscv/nixos/ltp/build_ltp.sh'
```

The build fails unless the selected runtime manifest contains exactly 767
entries.
It writes the full rootfs to `target/ltp/rootfs/`,
the initramfs to `target/ltp/ltp-initramfs.cpio.gz`,
and explicit omission reasons to `target/ltp/unavailable-tests.json`.

Run the host-only checks before starting QEMU:

```bash
make test_riscv_ltp_unit
```

Use the `asterinas-env:uboot-sim` container for preparation and QEMU when the
host does not provide the required RISC-V U-Boot toolchain:

```bash
docker run --rm -it --network=host \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas-env:uboot-sim bash
```

The remaining commands in this guide run inside that shell or on an equivalent
host environment.

## Baseline and strict modes

Record the full SMP=1 baseline without rebuilding LTP:

```bash
python3 tools/riscv/ltp_gate.py run \
  --kernel target/osdk/aster-kernel-osdk-bin.Image \
  --smp 1 --run-id baseline-m1-smp1 --skip-build --baseline
```

`--baseline` still requires the complete boot infrastructure to pass,
including artifact validation, the terminal guest marker,
and QEMU process-group cleanup.
Individual LTP failures are recorded but do not make the command return
nonzero.

The default mode is strict.
It returns nonzero for either an infrastructure failure or any LTP failure:

```bash
python3 tools/riscv/ltp_gate.py run \
  --kernel target/osdk/aster-kernel-osdk-bin.Image \
  --smp 1 --run-id strict-smp1 --skip-build
```

Run the five-test SMP=4 smoke before a full SMP=4 baseline:

```bash
python3 tools/riscv/ltp_gate.py run \
  --kernel target/osdk/aster-kernel-osdk-bin.Image \
  --smp 4 --run-id baseline-m1-smp4-smoke --skip-build --baseline \
  --boot-timeout 600 \
  --tag getpid01 --tag read01 --tag write01 \
  --tag uname01 --tag clock_gettime01
```

If the smoke completes without a kernel panic or hang,
record the full SMP=4 baseline:

```bash
python3 tools/riscv/ltp_gate.py run \
  --kernel target/osdk/aster-kernel-osdk-bin.Image \
  --smp 4 --run-id baseline-m1-smp4 --skip-build --baseline \
  --boot-timeout 7200
```

Every run ID is immutable and may contain only letters, digits, dots,
underscores, and hyphens.
Choose a new ID for every rerun.

## Results and count semantics

Each run writes serial output, marker evidence, boot status, normalized LTP
results, a human summary, the selected manifest,
and repository-relative checksums below `target/ltp/results/<run-id>/`.
Validate one completed result from the repository root:

```bash
python3 -m json.tool \
  target/ltp/results/baseline-m1-smp1/result.json >/dev/null
sha256sum -c target/ltp/results/baseline-m1-smp1/SHA256SUMS
```

The `result.json` counters are mutually exclusive.
`fail` counts plain FAIL/TBROK results only;
`crash` and `timeout` have their own counters.
`legacy_fail_total` equals `fail + crash + timeout`
and exists only for comparison with historical runner summaries.
The invariant is:

```text
total = pass + fail + conf + crash + timeout
```

## Evidence provenance

Everything below `target/` is ignored build or runtime evidence.
Do not commit generated rootfs files, boot disks, serial logs, or result JSON.
After a real baseline is complete and its checksums have been verified,
record the source branch and commit, LTP tag, active and packaged counts,
normalized verdicts, artifact hashes, and result-directory names in
`tools/riscv/ltp/BASELINE-M1-report.md`.

Historical results from another branch are provenance for the test selection,
not the current baseline.
Never copy their verdict counts into the tracked report;
derive every reported number from the current run's `result.json`.
