# QEMU futex/ppoll regression gates

These are bounded kernel/user regression runs used to interpret the Firefox r12 wait-chain evidence. No Firefox, DRM, or real hardware was started.

| Gate | Command | Result |
|---|---|---|
| pthread condition handoff | `make run_kernel AUTO_TEST=pthread_cond_handoff TARGET_ARCH=riscv64 SMP=4` | `pthread_cond_handoff: passed 4096 handoffs` / `pthread condition handoff regression passed.` |
| TCP ppoll wakeup | `make run_kernel AUTO_TEST=tcp_ppoll_wakeup TARGET_ARCH=riscv64 SMP=4` | `TCP_PPOLL case=nagle-default result=PASS`; `TCP_PPOLL case=tcp-nodelay result=PASS`; `TCP ppoll wakeup regression passed.` |

The current kernel image used by the runs is `/mnt/shared/xaj/Program/asterinas/target/osdk/aster-kernel-osdk-bin.Image` with SHA-256 `2f4a9a848b9fb4829c84c76c2e23e75c19aaa5b44779480d96f1caaedb1f9ba6`. These gates show that the basic futex condition handoff and TCP ppoll readiness paths pass independently; they do not prove Firefox renderer startup or WebRender.
