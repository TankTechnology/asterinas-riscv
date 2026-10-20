# GMAC-1 timeout evidence: bounded host gate

## Scope and checkout

This stage tightens the host timeout-evidence contract. It does not qualify
the Asterinas guest, a QEMU device model, or physical GMAC-1 access.

- Worktree: `/mnt/shared/xaj/Program/asterinas`.
- Branch: `dns-shim-isolated-console-fix`.
- HEAD before and after this stage: `0b8cbebde`.
- Latest commit: `feat(riscv): qualify recoverable fbdev desktop boot`.
- Existing status contained 158 modified/untracked entries before the stage.
- The SHA-256 of the original tracked dirty diff, excluding the two files
  changed in this stage, was identical before and after:
  `6aa52a58e8dba557a9ed5f3ce3a98d8c5042a2cb2e48206202aed70aad8bb726`.

No physical MMIO, serial access, hardware restart, reset/clean, commit, or push
was performed in this stage. Existing uncommitted changes were retained.

## Reproduced defect

`validate_probe_timeout_evidence()` accepted a pending offset merely because
it appeared somewhere in the register whitelist. It also accepted multiple
pending attempts and attempt/read records in impossible execution order.
Consequently, the timeout classifier could identify a skipped or repeated
register as the location of a stalled load.

The GMAC-1 probe performs synchronous reads. A blocked read cannot emit an
attempt for another register. A valid timeout transcript must therefore have:

1. Completed reads that form a prefix of the six-register whitelist.
2. Attempts for exactly those reads, optionally followed by one attempt for
   the next whitelist register.
3. Each completed read after its attempt and before the next attempt.
4. No attempt beyond the end of the whitelist.

Existing recovery requirements and the terminal `timeout` classification
remain in effect. Timeout evidence cannot become successful probe evidence.

## Changes

- `tools/riscv/megrez_hardware_probe.py`: enforce the attempt prefix, pending
  count, and chronological attempt/read pairing for timeout evidence.
- `tools/riscv/tests/test_megrez_hardware_probe.py`: make the timeout fixture
  emit the kernel's interleaved records; cover all 13 valid GMAC-1 prefixes
  and 8 invalid skipped, repeated, extra, or reordered traces.

## Fresh verification

The focused pre-fix run used the persistent development container:

```sh
tools/docker/run_dev_container.sh --offline -- python3 -m unittest -v \
  tools.riscv.tests.test_megrez_hardware_probe
```

Result before the implementation change: 24 test methods, 8 expected failing
subcases, exit 1. All failures were `ProbeContractError not raised`, proving
that the old validator accepted the malformed traces. The valid-prefix cases
passed before and after the fix.

The post-fix run used the same persistent container:

```sh
tools/docker/run_dev_container.sh --offline -- python3 -m unittest -v \
  tools.riscv.tests.test_megrez_hardware_probe \
  tools.riscv.tests.test_megrez_gmac_gate \
  tools.riscv.tests.test_megrez_boot_menu \
  tools.riscv.tests.test_qemu_uboot_matrix
git diff --check -- tools/riscv/megrez_hardware_probe.py \
  tools/riscv/tests/test_megrez_hardware_probe.py
```

Result: 95 test methods passed in 1.781 seconds, exit 0. The targeted diff
check passed. Argparse error output in the GMAC suite came from existing
negative argument tests; those tests passed. Board-operation and QEMU-matrix
tests use fakes/mocks and do not start a physical boot or a QEMU guest.

## QEMU source-contract stage

The EIC7700 qtest source was audited without starting QEMU. Its
`readonly_offsets` array is exactly:

```text
0x0000, 0x00b0, 0x0110, 0x0120, 0x1000, 0x1008
```

The source also contains the four required fail-closed scenarios:

- a clock-gated read/write check returning `UINT32_MAX`;
- the six-register read-only probe and bounded repeated reads;
- the platform clock/HSP/reset sequence that makes GMAC-1 readable;
- reset assertion that closes the GMAC-1 gate again.

`tools/riscv/qemu_gmac_contract.py` now parses those declarations and checks
that the QEMU bases and whitelist agree with `PROBE_SPECS`, while requiring
all four scenarios. `tools/riscv/tests/test_qemu_gmac_contract.py` covers the
parser, reordered/missing whitelist rejection, and the checked source when it
is present at the standard host path. This is a source-level QEMU gate; it
does not claim that the qtest executable ran.

Fresh host verification for this stage:

```sh
python3 -m unittest -v \
  tools.riscv.tests.test_qemu_gmac_contract \
  tools.riscv.tests.test_megrez_hardware_probe \
  tools.riscv.tests.test_megrez_gmac_contract \
  tools.riscv.tests.test_qemu_uboot_matrix
```

Result: 51 tests passed, exit 0. A wider direct-host selection also reached
the GMAC gate tests but three fixture-lifecycle cases could not open a local
listening socket (`PermissionError: Operation not permitted`); those are
host sandbox restrictions, not GMAC contract failures. The earlier persistent
container run remains the authoritative 95-test result for the broader fake
operation suite.

The QEMU qtest binary itself is not runnable in this host environment:
`eic7700-dwc3-test` requires `GLIBC_2.38`, while the host provides glibc 2.35;
the Docker socket is also inaccessible to the current user. No kernel/QEMU
rebuild, physical MMIO, serial access, hardware restart, reset/clean, commit,
or push was performed. The next bounded stage remains an actual qtest run in
a compatible container; until then no runtime QEMU or physical GMAC-1 claim
is made.
