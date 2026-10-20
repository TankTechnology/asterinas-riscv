# Futex wake identity instrumentation

The Firefox r16 receipt showed many `ASTERINAS_FUTEX_WAKE_DETAIL` records with
`woken=0`, but the old marker did not identify the caller.  The diagnostic
marker now includes the current process and thread IDs:

```text
ASTERINAS_FUTEX_WAKE_DETAIL event=... pid=... tid=... addr=... bitset=...
```

This is an opt-in diagnostic change under `asterinas.futex_profile=1`; futex
keying, queue operations, wake counts, and return values are unchanged.

## Verification

```text
tools/docker/run_dev_container.sh -- make kernel \
  TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
Finished `dev` profile
git diff --check -- kernel/src/process/posix_thread/futex.rs
```

Kernel artifact SHA-256 after the change:

`0510a722cd317911013387a2a6f983733147319852b610d8c6628a5c465a8b5a`

No Firefox or hardware run was repeated for this instrumentation-only change.
