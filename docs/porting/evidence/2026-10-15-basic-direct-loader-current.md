# Basic direct-loader and provider handoff audit — 2026-10-15

This receipt records the host-side recheck after the Basic menu path was
changed to use the guarded artifact loader. The existing dirty worktree was
preserved; no reset, clean, commit, push, serial write, hardware MMIO access,
or physical reboot was performed during this recheck.

## Current host gates

| Gate | Result | Evidence |
|---|---|---|
| Basic loader and recovery host tests | PASS | `PYTHONPATH=tools/riscv:. python3 -m unittest -q tools.riscv.tests.test_megrez_boot_menu tools.riscv.tests.test_megrez_recovery_epoch tools.riscv.tests.test_megrez_board_session tools.riscv.tests.test_megrez_hardware_probe tools.riscv.tests.test_qemu_uboot_booti tools.riscv.tests.test_qemu_uboot_contracts`; 394 tests, all passed. |
| Direct-loader integration | PASS | `test_megrez_boot_menu.BasicLoaderIntegrationTests` exercises `fdt set`/readback for the five disabled nodes, removes and reads back `/chosen/asterinas,usb-host`, and stops before `booti` on ineffective edits. |
| Provider resolver/fallback contract | PASS | `tools.riscv.tests.test_debian_browser_web` + `tools.riscv.tests.test_debian_rootfs`; 298 tests, all passed. |
| Shell/static checks | PASS | `bash -n` for the display-provider, M3, M4 and M5 scripts; `git diff --check`. |

The relevant current-source hashes are:

```text
91f423f72524ebf720d0ed1bc7681e02894c27bf3d6e449149273b0d3f5adb2d  tools/riscv/megrez_menu_board.py
a44c5240f4cf0a6a36888ef8c7639b42c320f6ca6e65fc040892072db64f23  tools/riscv/megrez_boot_menu.py
6087d702b9ca0f1f77488ef8c4e8071935f7d07bf348e38587da5da652eaae8  tools/riscv/tests/test_megrez_boot_menu.py
63af7ac1ca7624cfb8af32444e7049e5cb3b95643b4cfc8a0a0e02e4f6f6a2f6  tools/riscv/tests/test_megrez_recovery_epoch.py
0f557e6b09cfc12ca468a2b80c2de4ce798cc1b40691ca8cac76776f6023399c  kernel/comps/dwmac/src/arch/riscv.rs
0113e7dbf97b0061ce0e747ab95b1e81d1dfde8d358a47dbbebb01000e0a91b5  kernel/comps/usb/src/arch/riscv/mod.rs
```

## QEMU ktest receipts

The DWMAC and USB QEMU receipts in
`target/megrez-qemu-gates-20261015/` were produced after the same six source
hashes above were in place. They report:

```text
dwmac: test result ... 40 passed; 0 failed; All crates tested.
usb:   test result ... 35 passed; 0 failed; All crates tested.
```

The current host-side attempt to invoke `cargo osdk test` through the generic
container was not counted as a new ktest receipt: that wrapper selected the
default x86 `q35` machine and stopped at
`qemu-system-riscv64: unsupported machine type: "q35"`. The existing RISC-V
ktest logs use the repository's RISC-V `virt` runner and remain the valid
QEMU evidence for this gate.

## Physical evidence

The required one-shot physical Basic run was already completed after the
host gates, in
`target/megrez-physical-step1/dt-status-basic-20261015-r2/physical-basic-final/`.
Its `result.json` records:

```text
passed=true
recovered=true
basic_device_gate.forbidden_registrations=[]
recovery_boundary=uboot-prompt
```

The serial receipt contains the five `status = "disabled"` readbacks,
`/chosen` without `asterinas,usb-host`, the Basic marker, and the subsequent
fresh OpenSBI → U-Boot recovery epoch. The loader and DWMAC/USB source hashes
listed above are the same hashes recorded by the r2 receipt; the r2 manifest
also records the exact kernel artifact hash used for that run. The board was
left at the recovered U-Boot prompt; no second physical run is warranted for
this unchanged path.

## Provider handoff change

The display provider resolver now validates the selected configuration and
runtime handoff before returning `fbdev` or `drm`. M4 and offline M5 bound an
Xorg start to 120 polls and permit one explicit `drm → fbdev` fallback. A
failed fallback is propagated, and a successful handoff records an
`ASTERINAS_DISPLAY_PROVIDER_RECEIPT` marker. No unbounded retry loop was
added.
