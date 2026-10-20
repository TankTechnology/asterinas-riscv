# ppoll boundary instrumentation r1

Date: 2026-10-20
Gate: `firefox-ppoll-boundary-instrumentation`
Claim: E3 (supplemental diagnostic; does not satisfy `official_firefox_scene`)

## Change

`kernel/src/syscall/ppoll.rs` now has an opt-in `asterinas.ppoll_profile=1`
trace with optional `asterinas.ppoll_detail_min_pid=N`. It emits bounded
`ASTERINAS_PPOLL_PROFILE` entry and return/error records with PID/TID, nfds and
timeout. The default path is unchanged and no syscall result is rewritten.

## Verification

* `rustfmt --edition 2024 --check kernel/src/syscall/ppoll.rs` — pass.
* `make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode` in the
  persistent `asterinas-qemu-gl-env-r2` container — pass; RISC-V kernel image
  built, with only pre-existing warnings.

No QEMU guest, Firefox scenario, or hardware operation was run in this step.
