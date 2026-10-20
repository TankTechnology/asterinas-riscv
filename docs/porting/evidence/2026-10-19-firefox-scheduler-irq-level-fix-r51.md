# Firefox scheduler IRQ-level fix r51

## Root cause hypothesis

The current dirty tree was clearing the per-CPU interrupt-level cell from
`ostd/src/task/processor.rs::after_switching_to()`. Historical commit
`890450259` explicitly removed that speculative reset. A task switch must not
undo state owned by the interrupt stack; doing so can change scheduler/IRQ
state while Firefox is creating its IPC threads.

## Change

Removed only the cross-task `take_level_for_switch()` reset and its now-unused
wrappers. All other existing diagnostics remain. Added a source contract test:

```text
tools/riscv/tests/test_scheduler_irq_level_contract.py
```

The test was observed failing before the change and passing afterward.

## Verification

```text
python3 -m unittest \
  tools.riscv.tests.test_scheduler_irq_level_contract \
  tools.riscv.tests.test_firefox_drm_scene_gate \
  tools.riscv.tests.test_debian_dev_overlay -q
Ran 54 tests ... OK

make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
Finished dev profile ...

make run_kernel TARGET_ARCH=riscv64 SMP=2 \
  AUTO_TEST=pthread_cond_handoff ENABLE_KVM=0
pthread_cond_handoff: passed 4096 handoffs
pthread condition handoff regression passed.
```

Kernel SHA-256 after the fix:

```text
503b516329598dfcb72934ccd4d7cefc12bca54390b56e27727c6fe7e0a4f22e
```

This is a kernel/scheduler regression fix and short futex handoff evidence.
It does not yet claim Firefox WebDriver or WebRender success; that requires a
separate bounded Firefox probe after this kernel is materialized.
