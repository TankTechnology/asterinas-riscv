# ppoll return-counter instrumentation r2

Date: 2026-10-20
Gate: `firefox-ppoll-return-counters`

This is supplemental E3 diagnostic evidence.  It does not claim that the
Firefox scene passed and does not change ppoll semantics.

## Change

When `asterinas.ppoll_profile=1` is enabled and the PID threshold admits the
call, `kernel/src/syscall/ppoll.rs` now counts three bounded result classes:

* `ret_zero`: `ppoll` returned zero ready descriptors;
* `ret_ready`: `ppoll` returned one or more ready descriptors;
* `ret_error`: the poll operation returned an error.

The counters are included only in the existing sampled return/error log lines.
The default profile is disabled, so ordinary syscall behavior and logging are
unchanged.

## Verification

* `rustfmt --edition 2024 --check kernel/src/syscall/ppoll.rs` — pass.
* `make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode` in the
  persistent `asterinas-qemu-gl-env-r2` container — pass; the resulting Image
  SHA-256 is `ee5959bddb72609881a97edb3768f1743d35e7735f5dc527f4cacd366ddb54db`.
* No QEMU guest, Firefox scene, serial, or hardware run was performed.

The next bounded probe can distinguish repeated zero-result polling from
ready/error returns without another long scene gate.

