# RISC-V Svade CI Coverage Design

## Status

The user approved the minimal PR3 direction on 2026-07-29. PR3 remains local
and uncommitted until user review.

## Goal

Keep one four-hart end-to-end boot regression for each supported RISC-V paging
mode while forcing QEMU to use software-managed A/D bits:

- Sv48 with Svade;
- Sv39 with Svade.

## Design

Reuse the existing RISC-V integration-test matrix, composite action, Makefile,
OSDK scheme, and QEMU argument generator.

`tools/qemu_args.sh` gains one documented environment input,
`RISCV_QEMU_CPU`, whose default is exactly the current
`rv64,svpbmt=true,zkr=true` CPU string. The RISC-V workflow adds two debug boot
matrix entries and explicitly sets `release: false`, following the existing
x86 debug-matrix pattern. Both select
`rv64,svpbmt=true,zkr=true,svadu=false,svade=true`. The Sv39 entry additionally
sets the existing `riscv_sv39_mode` Cargo feature and disables the CPU's Sv48
capability. This keeps the hardware-selected early page-table mode consistent
with the compiled OSTD paging mode.

The workflow step exports the optional matrix values as `RISCV_QEMU_CPU` and
`FEATURES`. The existing composite action invokes `make run_kernel`, the
Makefile already forwards `FEATURES`, and the existing OSDK RISC-V scheme
evaluates `tools/qemu_args.sh`. The workflow also forwards the matrix `smp`
value through the composite action's existing input. No new action input or
Makefile branch is needed.

## Scope

Modify only:

- `.github/workflows/test_riscv.yml`;
- `tools/qemu_args.sh`.

Do not add Python, a new test harness, an OSDK scheme, a Makefile target, or
kernel/OSTD behavior.

## Compatibility

- An unset or empty `RISCV_QEMU_CPU` produces the byte-for-byte existing CPU
  argument.
- Existing RISC-V matrix entries have no `features` or `riscv_qemu_cpu`
  member, so their environment values are empty and defaults remain active.
- The new entries explicitly select debug builds instead of relying on the
  composite action's missing-value expression.
- Sv39 continues to use the existing `riscv_sv39_mode` feature, with
  `sv48=false` ensuring that the RISC-V boot assembly selects the same paging
  mode on hardware.
- Failure diagnostics continue through the existing QEMU serial log, panic,
  and backtrace paths.

## Validation

1. Observe the baseline reject a requested forced-Svade CPU value.
2. Verify the default and overridden QEMU CPU strings after implementation.
3. Parse the workflow and assert that exactly the two intended forced-Svade
   entries exist with `SMP=4`, and only Sv39 enables `riscv_sv39_mode`.
4. Run both workflow-equivalent boot commands and require `qemu.log` to
   contain `Successfully booted.`. With the existing `hvc0` console,
   userspace output is written through the QEMU mux rather than
   `qemu-serial.log`. Use isolated OSDK build artifacts for each paging mode:
   locally switching from Sv39 back to an empty feature set can otherwise
   reuse the previous generated binary, while GitHub matrix jobs are isolated.
5. Run `make check TARGET_ARCH=riscv64`, shell syntax checking, formatting,
   and diff checks.
