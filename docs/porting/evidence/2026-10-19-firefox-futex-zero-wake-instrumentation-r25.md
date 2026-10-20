# Futex zero-result wake instrumentation r25

The kernel diagnostic path now records bounded futex wake attempts with
`woken=0` as well as positive wakes.  A separate log counter preserves the
128-record budget while keeping event numbering intact.  This makes the next
short Firefox probe able to distinguish an unmatched wake from a wake path
that was never reached.

No QEMU or Firefox runtime was started in this step.

## Verification

```text
tools/docker/run_dev_container.sh --workspace /mnt/shared/xaj/Program/asterinas \
  -- make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
Finished `dev` profile ...
python3 -m unittest tools.riscv.tests.test_firefox_drm_scene_gate \
  tools.riscv.tests.test_debian_dev_overlay -q
Ran 50 tests ...
OK
```

Kernel image SHA-256: `d29a0f0c74f2d3350ecb07b4ad3c9112c9eda158d7bce885fce0f185e41cce6c`

