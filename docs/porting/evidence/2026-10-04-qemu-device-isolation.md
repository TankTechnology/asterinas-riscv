# Current-source QEMU device isolation

Date: 2026-10-04 (Asia/Shanghai)

All runs below use the current-source image
`target/virtio-network-init-osdk/aster-kernel-osdk-bin.Image` with SHA-256
`0525e34578f9dc6933d8a409e76150aa76bc8db7f88178a90e424a36b2a49f31` and the
fixed timer-recovery initramfs. Each successful run is a fresh
`OpenSBI -> U-Boot -> booti -> Asterinas -> userspace -> SBI software reboot`
epoch; QEMU process-group cleanup completed.

| Isolation leg | Result | Recovery | Evidence |
| --- | --- | ---: | --- |
| QEMU virt baseline, explicit `-nic none` | PASS | 11.308 s | `target/qemu-uboot/software-reboot/current-source-qemu-virt-no-default-net-20261004/` |
| QEMU virt + virtio-net/slirp, explicit `-nic none` | PASS | 11.232 s | `target/qemu-uboot/software-reboot/current-source-qemu-virt-net-no-default-net-20261004/` |
| QEMU virt + PCI xHCI + USB keyboard, explicit `-nic none` | PASS | 10.928 s | `target/qemu-uboot/software-reboot/current-source-qemu-virt-xhci-no-default-net-20261004/` |
| SiFive-U MMC baseline, explicit `-nic none` | PASS | 9.343 s | `target/qemu-uboot/software-reboot/current-source-sifive-u-minimal-no-default-net-20261004-r8/` |
| SiFive-U built-in GEM + explicit user networking | PASS | 11.371 s | `target/qemu-uboot/software-reboot/current-source-sifive-u-explicit-gem-20261004/` |
| SiFive-U + requested QEMU xHCI | ERROR before boot | — | `target/qemu-uboot/software-reboot/current-source-sifive-u-xhci-20261004/` |

The SiFive-U QEMU model includes a FU540 GEM node. Its U-Boot firmware epoch
enumerates the Ethernet controller and performs bounded DHCP/TFTP attempts,
but Asterinas' current EIC7700 DWMAC driver intentionally does not bind to
that incompatible `sifive,fu540-c000-gem` node. Therefore the SiFive-U run is
useful for firmware/device-tree compatibility and recovery, while the
kernel-side network datapath is covered by the separate virtio-net leg.

QEMU reports `No 'PCI' bus found for device 'qemu-xhci'` when xHCI is attached
to SiFive-U. The harness records this as a pre-boot error with zero `booti`
commands and complete cleanup. It is evidence that the device combination is
invalid for that QEMU machine, not evidence of an Asterinas USB failure.

The runner now appends `-nic none` to every device set that does not explicitly
render a network backend. This closes QEMU's implicit user-network default:
the headless leg is UART/timer/storage plus no network backend, while the
virtio-net leg contains only its explicit slirp backend. The three reruns above
all retained `passed=true`, a single boot command, a single recovery epoch, and
`cleanup_complete=true` in `result.json`.

The SiFive-U board model's U-Boot has a default network boot fallback even
when QEMU has no network backend. The registered SiFive-U machine contract now
sends one guarded `Ctrl-C` after the U-Boot banner in each firmware epoch;
this interrupts that fallback without adding a command to the boot script.
The strict MMC baseline then completed the same software-recovery contract
with `-nic none`; its retained serial log contains zero BOOTP, DHCP, or TFTP
attempts. The QEMU model still enumerates its built-in FU540 GEM node, so this
proves the firmware/kernel path with no network backend, not a physical EIC7700
GMAC datapath or DMA test.

The explicit GEM leg uses the registered `sifive-u-gem-timer-recovery` profile
and `sifive-u-gem-slirp` device set. It records four bounded DHCP/TFTP attempts
from U-Boot in each firmware epoch, then completes Asterinas boot and software
recovery. This isolates the QEMU GEM firmware/network backend from the generic
virtio-net leg. Asterinas still reports no EIC7700 DWMAC match on this FU540
device tree, so kernel-side GEM/DWMAC interrupt and DMA behavior remains an
unverified hardware-specific boundary.

A second bounded matrix was run after the device-contract changes. The QEMU
virt minimal, virtio-net, and xHCI legs recovered in 11.235 s, 11.244 s, and
11.016 s; the SiFive-U network-free MMC and explicit GEM legs recovered in
9.362 s and 11.337 s. Every repeat recorded exactly one `booti`,
`failure=null`, and `cleanup_complete=true`:

```text
target/qemu-uboot/software-reboot/repeat-qemu-virt-minimal-20261004/
target/qemu-uboot/software-reboot/repeat-qemu-virt-net-20261004/
target/qemu-uboot/software-reboot/repeat-qemu-virt-xhci-20261004/
target/qemu-uboot/software-reboot/repeat-sifive-u-minimal-20261004/
target/qemu-uboot/software-reboot/repeat-sifive-u-gem-20261004/
```

The new one-click matrix runner repeated the same five isolation contracts
from the audited timer-recovery bundles in
`target/qemu-uboot/matrix-container-20261004-r5/`. It passed all five legs in
one sequential invocation: QEMU virt headless, virtio-net, xHCI, SiFive-U
MMC, and SiFive-U built-in GEM. The runner recorded one `booti`, a fresh
OpenSBI/U-Boot recovery epoch, and complete cleanup for every leg. Ordinary
smoke bundles do not satisfy the runtime timer-marker/recovery contract.
The Make entry point requires explicit bundle directories and checks files
exist; directory selection alone does not attest the payload's purpose.

With `--repeat 2`, the same matrix passed all ten runs in
`target/qemu-uboot/matrix-container-20261004-r6-repeat2/`; each result kept
`failure=null` and `cleanup_complete=true`. The SiFive-U GEM row remains a
firmware/model cross-check: QEMU has no EIC7700 GMAC datapath, interrupt, clock,
or DMA model.

An inventory of the container's QEMU 10.2.1 binary confirms there is no
`dwmac`, `stmmac`, or EIC7700 GMAC device to add to this matrix. The next
hardware-specific step therefore cannot be made truthful by adding another
generic virtual NIC; it requires either an EIC7700 QEMU model or a recovered
physical board. The capability-aware runner records this inventory before
launching any leg; the headless probe is retained at
`target/qemu-uboot/matrix-container-20261004-r8-capability-v2/`.
