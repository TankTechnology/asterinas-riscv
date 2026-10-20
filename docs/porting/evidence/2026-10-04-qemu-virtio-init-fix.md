# QEMU SiFive-U virtio initialization fix

Date: 2026-10-04 (Asia/Shanghai)

The current-source SiFive-U MMC recovery run exposed a startup panic before
rootfs. `aster-virtio` initialized network and socket DMA pools before it had
enumerated any transport. On the SiFive-U machine this caused
`DmaPage::new(...).unwrap()` to return `AccessDenied` at
`kernel/comps/network/src/dma_pool.rs:73`.

The initialization contract is now device-scoped:

- the socket registry is still initialized unconditionally, because the later
  vsock layer safely queries it even when no socket device exists;
- virtio-net pools are initialized only in the `VirtioDeviceType::Network`
  arm;
- virtio-vsock pools are initialized only in the `VirtioDeviceType::Socket`
  arm.

`tools/riscv/tests/test_virtio_network_init_contract.py` was written first and
failed against the eager initialization. It now checks both guarded pool
sites and the unconditional socket registry. `rustfmt`, Ruff, and the test
pass; a current-source `cargo check -p aster-kernel --target
riscv64imac-unknown-none-elf` also passes with only existing warnings.

The rebuilt OSDK image is
`target/virtio-network-init-osdk/aster-kernel-osdk-bin.Image`, SHA-256
`0525e34578f9dc6933d8a409e76150aa76bc8db7f88178a90e424a36b2a49f31`. The
fresh SiFive-U run is:

```text
target/qemu-uboot/software-reboot/current-source-sifive-u-recovery-20261004-r5/result.json
passed=true
profile=sifive-u-asterinas-timer-recovery
```

It reached the timer userspace marker, observed the SBI software reboot, and
reached the second OpenSBI/U-Boot prompt within the bounded recovery window.
The run retained its serial log and artifact manifest and left no QEMU
process. This validates the emulated UART/timer/MMC/ext4 path only; it does
not qualify the physical EIC7700 board or its GMAC/USB controllers.
