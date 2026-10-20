# QEMU Asterinas software-recovery run (2026-10-11)

This run used the registered `qemu-virt-asterinas-timer-recovery` profile and
the guarded U-Boot `booti` runner. It was entirely QEMU-hosted; no physical
serial device, board MMIO, or network traffic was used.

Evidence is under `target/qemu-uboot/final-recovery-20261011/`:

- `result.json` reports `status=PASS`, `passed=true`, and
  `session.recovery_complete=true`.
- `serial.log` contains one Asterinas recovery marker, two OpenSBI epochs, and
  three U-Boot version occurrences (the guarded `version` command accounts for
  the extra line).
- The second epoch reaches a fresh U-Boot prompt, so the recovery state is
  `uboot-prompt`. This is not a RockOS boot: the QEMU virt bundle has no RockOS
  image, and the state must not be promoted to `rockos-login`.

The run proves Asterinas → software reboot → fresh firmware/U-Boot recovery.
RockOS fallback remains an explicitly separate evidence item requiring a
RockOS payload or a physical management-chain run.
