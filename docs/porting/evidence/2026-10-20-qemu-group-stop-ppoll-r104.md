# QEMU focused group-stop ppoll regression r104

- gate: `e3-ppoll-signal-semantics`
- claim: `E3` diagnostic only; this does not satisfy the official Firefox scene.
- status: `pass`
- command: `make run_kernel AUTO_TEST=regression REGRESSION_TEST_DIRS="[ "process" ]" TARGET_ARCH=riscv64 SMP=2 CARGO_OSDK_BUILD_ARGS="--kcmd-args=loglevel=info --kcmd-args=earlycon --kcmd-args=console=ttyS0 --init-args=/test/run_group_stop_ppoll_test.sh --scheme riscv --grub-boot-protocol=multiboot2"`

## Result

The focused wrapper ran only `/test/process/signal/group_stop_ppoll`. All six
cases passed (`GROUP_STOP_PPOLL passed=6/6`), including signal interruption,
SA_RESTART handling, timeout copyback, read-only timeout storage and a temporary
signal mask. No Firefox, Xorg, DRM, network load or real hardware was started.

The preceding process-only run also recorded an independent `sigtimedwait_race`
timeout at request 7938 (`done=7938`, `completed=7937`, `sender_phase=0`). That
failure is retained as a separate signal/semaphore diagnostic and is not
reclassified as a ppoll failure.

## Artifacts

- serial: `/mnt/shared/xaj/Program/asterinas/qemu-serial.log` (SHA-256 `c1ca0d7549a6953d70053a38fce01e3637e5c9e0e691348a1d977256a5cbdcb5`)
- kernel: `/mnt/shared/xaj/Program/asterinas/target/osdk/aster-kernel-osdk-bin.Image` (SHA-256 `38982e2e1b81b4bdd7649d9e9270022a705afd964ac77b66d5cacc26b1ae81be`)
- initramfs: `/mnt/shared/xaj/Program/asterinas/test/initramfs/build/initramfs.cpio.gz` (SHA-256 `f39d36c8b2bc63ca3668dcc58eb43a154aa00c252dfbbeaa1d503c9fe6207b1c`; resolved in the QEMU container)
- host QEMU log: `/mnt/shared/xaj/Program/asterinas/qemu.log` (SHA-256 `847230853c5db93a82e843c500dc616e73656c2002e1066eefe5c0fd22e060ec`)
- focused wrapper: `test/initramfs/src/regression/scripts/run_group_stop_ppoll_test.sh`

The next boundary remains the Firefox-specific cross-process `ppoll`/`futex`
wait chain; no full 900-second scene rerun is authorized by this diagnostic.
