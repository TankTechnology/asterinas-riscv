# RISC-V LTP gates

These gates run reviewed Linux Test Project syscall suites on the current
Asterinas RISC-V kernel through U-Boot and QEMU.
The LTP source is pinned to tag `20260529`.
The `syscalls` suite requests 779 names and packages exactly 767 tests.
The focused `arch-riscv64` suite requests and packages exactly 158 tests.
The current musl/GNU-UAPI cross build has no unavailable test in this suite.
Every suite reports omissions in `target/ltp/unavailable-tests.json`.
The build patches LTP's RISC-V clone helper to bypass musl's userspace-only
thread-flag rejection, ensuring `clone08` reaches the kernel under test.

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

The cross-build image provides the GNU RISC-V compiler but not its musl
wrapper and sysroot.
Download the pinned official Arch Linux package into the ignored toolchain
directory and verify its identity:

```bash
mkdir -p target/ltp/toolchain/package target/ltp/toolchain/root
curl --fail --location \
  --output target/ltp/toolchain/package/musl-riscv64-1.2.6-1-x86_64.pkg.tar.zst \
  https://archlinux.org/packages/extra/x86_64/musl-riscv64/download/
printf '%s  %s\n' \
  0797f54b48c415739bb5360739bc8f9dc8b2019e01de86d89c2859810200b589 \
  target/ltp/toolchain/package/musl-riscv64-1.2.6-1-x86_64.pkg.tar.zst \
  | sha256sum -c -
tar --extract \
  --file target/ltp/toolchain/package/musl-riscv64-1.2.6-1-x86_64.pkg.tar.zst \
  --directory target/ltp/toolchain/root
```

Build the Sv39 RISC-V kernel in the project cross-build container:
The image sets `VDSO_LIBRARY_DIR=/root/linux_vdso` and provides the matching
RISC-V binary;
the command verifies that image contract before compiling.

```bash
docker run --rm --privileged --network=host -v /dev:/dev \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc \
  bash -lc 'restore_owner() { chown -R --reference=/root/asterinas \
      /root/asterinas/target/osdk 2>/dev/null || true; }; \
    trap restore_owner EXIT; \
    test -s "${VDSO_LIBRARY_DIR}/vdso_riscv64.so"; \
    make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode'
```

The LTP image requires static BusyBox helpers for tests that invoke shell
commands. Build the pinned BusyBox artifact in the same cross image before
packaging LTP, and verify that it was published at the expected path:

```bash
tools/riscv/nixos/build_busybox.sh
test -x target/nixos/busybox
```

Build LTP in the same cross image,
mounting only the pinned wrapper and sysroot read-only:
The command installs the missing Autotools frontend in the ephemeral
container before configuring LTP.

```bash
docker run --rm --network=host \
  -v "$PWD:/root/asterinas" -w /root/asterinas \
  -v "$PWD/target/ltp/toolchain/root/usr/bin/riscv64-linux-musl-gcc:\
/usr/bin/riscv64-linux-musl-gcc:ro" \
  -v "$PWD/target/ltp/toolchain/root/usr/riscv64-linux-musl:\
/usr/riscv64-linux-musl:ro" \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc \
  bash -lc 'apt-get update -qq; \
    apt-get install -y --no-install-recommends \
      autoconf automake linux-libc-dev-riscv64-cross; \
    restore_owner() { chown -R --reference=/root/asterinas \
      /root/asterinas/target/ltp 2>/dev/null || true; }; \
    trap restore_owner EXIT; \
    tools/riscv/nixos/ltp/build_ltp.sh --suite syscalls'
```

To repackage the reviewed RISC-V architecture suite from existing LTP build
outputs, change the final command inside the same pinned container to:

```bash
tools/riscv/nixos/ltp/build_ltp.sh --skip-compile --suite arch-riscv64
```

The build fails unless the selected runtime manifest and omission evidence
match the named suite's exact count contract.
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
  --env ASTERINAS_SOURCE_COMMIT="$(git rev-parse HEAD)" \
  asterinas-env:uboot-sim bash
```

Passing the source commit explicitly also supports an isolated Git worktree,
whose administrative `.git/worktrees` directory is normally outside the bind
mount. The gate validates this value before starting QEMU.

The remaining commands in this guide run inside that shell or on an equivalent
host environment.

While a gate is running, inspect its current test and mutually exclusive
counts from another shell:

```bash
python3 tools/riscv/ltp_gate.py status --run-id arch-riscv64-m1-smp4
tail -f target/ltp/results/arch-riscv64-m1-smp4/progress.log
```

`progress.log` is a readable live mirror, not the authoritative evidence.
After the completion marker, the QEMU driver atomically publishes the protected
`serial.log`; the gate includes both files in `SHA256SUMS`.
The PID 1 shim emits `__LTP_GATE_TERMINAL__` after either normal completion or
an abnormal runner exit, so a broken runner is reported immediately instead of
waiting for the global QEMU timeout.

## Baseline and strict modes

Routine gates default to SMP=4. Record the focused architecture baseline with:

```bash
python3 tools/riscv/ltp_gate.py run \
  --kernel target/osdk/aster-kernel-osdk-bin.Image \
  --suite arch-riscv64 --smp 4 \
  --run-id arch-riscv64-m1-smp4 --skip-build --baseline \
  --boot-timeout 2400
```

Every `--skip-build` run must name the suite currently packaged in
`target/ltp/ltp-initramfs.cpio.gz`.
Before switching from the architecture examples below to a `syscalls` run,
repackage inside the pinned cross container with:

```bash
tools/riscv/nixos/ltp/build_ltp.sh --skip-compile --suite syscalls
```

For the full syscall suite, start with the five-test SMP=4 smoke run:

```bash
python3 tools/riscv/ltp_gate.py run \
  --kernel target/osdk/aster-kernel-osdk-bin.Image \
  --suite syscalls --smp 4 \
  --run-id baseline-m1-smp4-smoke --skip-build --baseline \
  --boot-timeout 600 \
  --tag getpid01 --tag read01 --tag write01 \
  --tag uname01 --tag clock_gettime01
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
  --suite syscalls --smp 4 --run-id strict-smp4 --skip-build
```

If the smoke completes without a kernel panic or hang,
record the full SMP=4 baseline:

```bash
python3 tools/riscv/ltp_gate.py run \
  --kernel target/osdk/aster-kernel-osdk-bin.Image \
  --suite syscalls --smp 4 \
  --run-id baseline-m1-smp4 --skip-build --baseline \
  --boot-timeout 7200
```

Use SMP=1 only as an explicit diagnostic override; it is not a paired
admission requirement:

```bash
RISCV_LTP_SMP=1 RISCV_LTP_SUITE=syscalls make test_riscv_ltp \
  ASTERINAS_RISCV_BOOTI=target/osdk/aster-kernel-osdk-bin.Image
```

Every run ID is immutable and may contain only letters, digits, dots,
underscores, and hyphens.
Choose a new ID for every rerun.
The prepared boot artifacts are isolated under
`target/ltp/qemu/smp<count>/<run-id>/`, so a later run cannot invalidate an
earlier run's checksums.

## Results and count semantics

Each run writes serial output, marker evidence, boot status, normalized LTP
results, a human summary, a run-owned initramfs, `package.json`,
`manifest.txt`, `unavailable-tests.json`,
and repository-relative checksums below `target/ltp/results/<run-id>/`.
Validate one completed result from the repository root:

```bash
python3 -m json.tool \
  target/ltp/results/baseline-m1-smp4/result.json >/dev/null
sha256sum -c target/ltp/results/baseline-m1-smp4/SHA256SUMS
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
the suite-specific report under `tools/riscv/ltp/`.

Historical results from another branch are provenance for the test selection,
not the current baseline.
Never copy their verdict counts into the tracked report;
derive every reported number from the current run's `result.json`.
