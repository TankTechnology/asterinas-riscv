# Devicetree status gates for GMAC and DWC3 (2026-10-14)

This change was validated only in the host and RISC-V QEMU ktest environment;
no physical-board MMIO, serial input, or reboot was performed.

## Gate changes

- GMAC now interprets missing/`ok`/`okay` status as enabled and treats every
  other status as disabled.
- GMAC discovery returns `Ok(None)` before MMIO acquisition, clock programming,
  reset GPIO access, PHY/MDIO, or DMA setup when either required EIC7700 port is
  disabled. A disabled node is therefore skipped instead of reported as an
  invalid device tree.
- DWC3 selected-host setup checks the selected node and all named ancestors.
  A disabled EIC7700 wrapper is rejected before `IoMem` acquisition.
- When both EIC7700 USB wrapper nodes are disabled, USB returns before PCI xHCI
  driver registration. QEMU's `virt` PCI fallback remains available because it
  has no EIC7700 wrapper nodes.

## Verification

Host checks:

```text
rustfmt --edition 2024 --check kernel/comps/dwmac/src/arch/riscv.rs
rustfmt --edition 2024 --check kernel/comps/usb/src/arch/riscv/mod.rs
cargo check -p aster-dwmac -p aster-usb --target riscv64imac-unknown-none-elf
```

All completed successfully. Existing unrelated warnings in the dirty branch
remain in `ostd` (`CpuId` unused and an unnecessary qualification).

Focused host contract tests passed:

```text
HardwareProbeTests.test_kernel_probe_paths_are_read_only_and_guard_normal_initializers
BootTransactionTests.test_minimal_device_profile_disables_usb_and_gmac_without_chosen_host
DesktopDeviceProfileTests (4 tests)
```

QEMU ktests passed:

- `aster-dwmac`: **38 passed, 0 failed**;
- `aster-usb`: **31 passed, 0 failed**.

The GMAC QEMU log includes the new
`skips_disabled_ports_before_mmio_validation` and standard status tests. The
USB QEMU log includes the standard status test and reports 31/31 success.

## Physical-board decision

The QEMU/host gate is now sufficient to schedule one bounded physical Basic
retest, but no such retest was started in this step. The next run must retain
the watchdog, use the same single serial owner, and stop if the Basic marker or
recovery boundary is absent. No GMAC, DWC3, desktop, or network load should be
added until that Basic run passes.
