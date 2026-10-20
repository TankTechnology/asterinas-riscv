# QEMU firmware-to-userspace recovery matrix

Dates: 2026-10-03 to 2026-10-04 (Asia/Shanghai)

This run was performed entirely inside the project development container. No
physical serial device, board reset, network management address, or boot
partition was accessed.

## Contract

The registered profile
`megrez-sv48-svade-timer-recovery` drives the sequence
`OpenSBI -> U-Boot -> booti -> Asterinas`. Its guest boot arguments arm the
ten-second software recovery timer. The terminal marker is
`ASTERINAS_REBOOT_TIMER_READY`; success additionally requires a new, ordered
`OpenSBI v1.7 -> U-Boot 2026.07 -> \n=> ` firmware epoch. The runner uses a
45-second recovery deadline, records one `booti` maximum, and terminates the
QEMU process group after the bounded run.

QEMU `virt` has no EIC7700 MMC, GMAC, or DWC3 model. The storage leg below is
therefore the virtio-block path. The device additions check coexistence during
boot and recovery, not sustained interrupt/DMA activity. They are not
physical-board qualification.

## Results

The `virt` positive runs used the same read-only source boot disk through the
runner's snapshot staging. The positive SiFive-U follow-up used a private SD
image whose kernel inode was replaced before the run; the runner still staged
that image read-only with a QEMU snapshot. Each result contains the immutable
artifact hashes, QEMU argv, serial log, marker event, and recovery timing.

| Run | Devices | Result | Recovery | Evidence |
| --- | --- | --- | --- | --- |
| `timer` | UART, timer, virtio-block | PASS | 12.340 s | `target/qemu-uboot/software-reboot/current-goal-20261003/timer/` |
| `r2` | UART, timer, virtio-block | PASS | 12.187 s | `target/qemu-uboot/software-reboot/current-goal-20261003/r2/` |
| `r3` | UART, timer, virtio-block | PASS | 12.187 s | `target/qemu-uboot/software-reboot/current-goal-20261003/r3/` |
| `virtio-net` | baseline plus virtio-net/slirp | PASS | 12.187 s | `target/qemu-uboot/software-reboot/current-goal-20261003/virtio-net/` |
| `qemu-xhci` | baseline plus QEMU xHCI and USB keyboard | PASS | 12.460 s | `target/qemu-uboot/software-reboot/current-goal-20261003/qemu-xhci/` |
| `qemu-xhci-input-gate` | PCI xHCI plus USB keyboard and mouse HID events | PASS | bounded gate | `target/riscv-xhci-input/result.json` |
| `sifive-u` | SiFive-U UART plus MMC/ext4 | **FAIL** | not reached | `target/qemu-uboot/software-reboot/current-goal-20261003/sifive-u/` |
| `sifive-u-current-kernel` | SiFive-U UART plus MMC/ext4, recovery-enabled Sv39 payload | PASS | 12.351 s | `target/qemu-uboot/software-reboot/sifive-u-current-kernel-20261004/` |
| `sifive-u-current-kernel-r2` | same private MMC image and recovery-enabled Sv39 payload | PASS | 12.322 s | `target/qemu-uboot/software-reboot/sifive-u-current-kernel-20261004-r2/` |
| `sifive-u-current-source-r5` | SiFive-U UART plus MMC/ext4, current-source virtio lazy-DMA fix | PASS | 11.355 s | `target/qemu-uboot/software-reboot/current-source-sifive-u-recovery-20261004-r5/` |
| `qemu-virt-current-source` | QEMU virt UART/timer/virtio-block, current-source recovery profile | PASS | 11.271 s | `target/qemu-uboot/software-reboot/current-source-qemu-virt-recovery-20261004/` |
| `qemu-virt-current-source-net` | same profile plus virtio-net/slirp | PASS | 11.283 s | `target/qemu-uboot/software-reboot/current-source-qemu-virt-net-recovery-20261004/` |
| `qemu-virt-current-source-xhci` | same profile plus PCI xHCI and USB keyboard | PASS | 10.931 s | `target/qemu-uboot/software-reboot/current-source-qemu-virt-xhci-recovery-20261004/` |
| `qemu-virt-current-source-no-default-net` | UART/timer/virtio-block with explicit `-nic none` | PASS | 11.308 s | `target/qemu-uboot/software-reboot/current-source-qemu-virt-no-default-net-20261004/` |
| `qemu-virt-current-source-net-no-default-net` | virtio-net/slirp plus explicit `-nic none` | PASS | 11.232 s | `target/qemu-uboot/software-reboot/current-source-qemu-virt-net-no-default-net-20261004/` |
| `qemu-virt-current-source-xhci-no-default-net` | PCI xHCI/USB keyboard plus explicit `-nic none` | PASS | 10.928 s | `target/qemu-uboot/software-reboot/current-source-qemu-virt-xhci-no-default-net-20261004/` |
| `sifive-u-current-source-minimal-no-default-net` | SiFive-U MMC/ext4 with explicit `-nic none` and guarded U-Boot autoboot interrupt | PASS | 9.343 s | `target/qemu-uboot/software-reboot/current-source-sifive-u-minimal-no-default-net-20261004-r8/` |
| `sifive-u-current-source-gem` | SiFive-U built-in FU540 GEM with explicit user networking and MMC/ext4 recovery | PASS | 11.371 s | `target/qemu-uboot/software-reboot/current-source-sifive-u-explicit-gem-20261004/` |
| `sifive-u-current-source-xhci` | SiFive-U plus QEMU xHCI request | **ERROR** | not started | `target/qemu-uboot/software-reboot/current-source-sifive-u-xhci-20261004/` |
| `debian-m5-qemu-slirp` | Debian root, virtio-net/slirp, 20 fixed 64 KiB transfers plus DNS/HTTPS | **FAIL** | protocol after panic | `target/debian-riscv/desktop-m5-network/qemu-gate-current-20261004/` |
| `debian-m5-qemu-slirp-atomic` | same Debian root and workload, rebuilt from the current source with allocation-free atomic-mode diagnostics | PASS | bounded gate | `target/debian-riscv/desktop-m5-network/qemu-gate-atomic-20261004/` |

The controlled negative run set `--boot-timeout 3`. It returned exit code 1,
classified the result as `FAIL/boot-timeout`, sent only one `booti`, reported
`cleanup_complete=true`, left no QEMU process, and retained all three primary
evidence files:
`target/qemu-uboot/software-reboot/current-goal-20261003/timeout-negative/`.

The controller regression suite also drives a terminal marker without any
post-terminal firmware epoch and verifies the automatic
`recovery-timeout:opensbi` classification, one cleanup pass, and no retry.

The `sifive_u` cross-check is a deliberately separate negative result. U-Boot
loaded the kernel, DTB, and initramfs from the emulated SD/MMC path, and
Asterinas reached `ASTERINAS_REBOOT_TIMER_READY`. The payload never emitted
`ASTERINAS_SOFTWARE_REBOOT_ARMED seconds=10`, so the runner timed out waiting
for a post-recovery OpenSBI epoch and terminated QEMU after retaining the
failure evidence. This proves the MMC boot path only through userspace for the
audited payload; it does not prove software recovery on the SiFive-U model.
The positive `virt` runs use kernel SHA-256
`21e7a65b276369b12ee0108fea03b19d78086027454ef158d015ba06969b72e3`, while
the SiFive-U run uses
`1c555ae50a35a04d09a52f9aea7ea7f74c9c552e076447f210d5328dc862ddda`.
This result must not be interpreted as a regression of the timer
implementation. The follow-up `sifive-u-current-kernel` run replaced only the
kernel in a private SD image with the audited Sv39 artifact
`699d18865013aee59b36c2e9cfaabdd80323e30ac511c165fa54ad470e7d8ecb`, kept the
same SiFive-U DTB and 550-byte marker initramfs, and passed the complete
recovery contract. A fresh `FEATURES=riscv_sv39_mode` build is still required
to bind this result to the current worktree source.

The QEMU/U-Boot unit suite ran 242 tests: 241 passed and one
environment-dependent test was skipped:

```text
make test_riscv_uboot_booti_unit
Ran 242 tests in 6.315s
OK (skipped=1)
```

The dedicated xHCI input gate also passed. Its result records PCI
`0000:00:01.0` (`1b36:000d`, interrupt 33, parent 9), one USB keyboard and
one USB mouse, the exact eight keyboard events and seven pointer events, and
`cleanup=complete`. QEMU used `-nic none`, `qemu-xhci` with MSI/MSI-X disabled,
and the gate's process-group cleanup. This is stronger than enumeration-only,
but still does not exercise the physical EIC7700 DWC3 controller.

The Debian M5 slirp run is retained as a failure diagnostic. Its fixture
server recorded exactly 20 successful 64 KiB requests, and the guest reached
the QEMU DNS and HTTPS markers (`www.baidu.com`, status 200). The same serial
log contains `MIGHT_SLEEP_VIOLATION`, allocator `RefCell already borrowed`, and
an atomic-mode panic at `kernel/comps/block/src/bio.rs`; the gate classified the
overall protocol as failed and cleaned up QEMU. This is evidence of a kernel
stability failure under the Debian/rootfs plus network workload, not evidence
that virtio-net is stable. The smaller timer and xHCI gates do not exercise
this block-heavy userspace workload.

The follow-up `debian-m5-qemu-slirp-atomic` run used the freshly built
`target/atomic-mode-osdk/aster-kernel-osdk-bin.Image` (kernel SHA-256
`6ccd523dafd177e4ba4a844e3cb5e4abc667141ef9825f370f7d4e7280e5b455`) with the
same frozen Debian root and U-Boot inputs. It recorded all 20 fixture requests,
DNS, HTTPS status 200, and the desktop M4 input/display markers, and published
`passed=true`. A scan of the retained serial log found no
`MIGHT_SLEEP_VIOLATION`, `ATOMIC_GUARD_TAKEN`, `RefCell already borrowed`,
`PANIC_ENTRY`, or `Uncaught panic` lines. This closes the diagnostic recursion
regression and shows that this bounded QEMU workload completes on the current
source image; it does not yet prove sustained physical-board stability or
identify every caller that can wait in atomic mode.

The current-source SiFive-U run initially failed before userspace because
`aster-virtio` eagerly allocated both virtio-net and virtio-vsock DMA pools on
a machine with no virtio network or socket transport; the first failure was
`AccessDenied` at `kernel/comps/network/src/dma_pool.rs:73`. The fix keeps the
socket registry initialized for later `get_device` calls but defers socket DMA
pool allocation, and defers the network pools, until the corresponding
transport arm is actually selected. A fresh image with SHA-256
`0525e34578f9dc6933d8a409e76150aa76bc8db7f88178a90e424a36b2a49f31` then
reached `ASTERINAS_REBOOT_TIMER_READY`, performed SBI software recovery, and
observed the second OpenSBI/U-Boot prompt. The result is the `r5` row above;
QEMU was cleaned up and no physical board was involved.

The current-source device isolation runs use the same kernel SHA-256
`0525e34578f9dc6933d8a409e76150aa76bc8db7f88178a90e424a36b2a49f31` and the
same 550-byte marker initramfs. The headless QEMU virt baseline, virtio-net
alone, and PCI xHCI plus USB keyboard each reached the timer userspace marker,
completed the SBI software recovery, and left `cleanup_complete=true`. The
SiFive-U model already contains the FU540 GEM Ethernet node; its U-Boot log
shows `eth0`, DHCP, and bounded TFTP attempts during the firmware epoch, while
the Asterinas EIC7700 DWMAC driver correctly finds no matching EIC7700
configuration. This is a board-model/firmware network cross-check, not a
kernel GMAC datapath result.

The strict replacement baseline uses explicit `-nic none`; its serial log has
zero BOOTP, DHCP, or TFTP lines. Because SiFive-U U-Boot otherwise enters a
network fallback before the prompt, the runner sends one guarded `Ctrl-C` after
each OpenSBI/U-Boot firmware epoch. This makes the MMC baseline genuinely
network-free while retaining the old network-enabled run as a firmware-only
cross-check.

Adding QEMU xHCI to the SiFive-U command line fails before OpenSBI starts with
`No 'PCI' bus found for device 'qemu-xhci'`. The runner records this as
`status=ERROR`, retains the serial log and full argv, sends zero `booti`
commands, and cleans up. USB recovery is therefore measured on QEMU virt,
where the PCI bus and xHCI model are valid; the SiFive-U rejection is kept as
a machine/device compatibility guard rather than treated as a driver failure.

## Interpretation

The timer/SBI recovery contract is repeatable in both the minimal QEMU `virt`
path and the SiFive-U MMC/ext4 path when the payload contains the recovery
implementation; the strict current-source SiFive-U baseline recovered in
9.343 s with `-nic none`, and the current-source lazy-DMA image also recovered
in the bounded `r5` run.
Adding the generic network device or QEMU xHCI did not change the recovery
result in this workload. U-Boot enumerated xHCI and its keyboard, but no
Asterinas USB transfer or input test was performed. The virtio-net run includes
kernel feature negotiation but no userspace traffic workload. These runs do
not exclude a driver failure under load. The old SiFive-U artifact is retained as a useful
negative control: it reached userspace but lacked the armed marker, and the
runner failed closed. The physical incident remains unresolved: these older
payloads and minimal workloads do not reproduce the current physical boot.
EIC7700 interrupt, DMA, clock, and reset paths remain outside this validation.
The next
physical attempt, after an operator reset and fresh RockOS attestation, must
still use the host-side `--device-profile minimal` board profile before
enabling GMAC or DWC3.
