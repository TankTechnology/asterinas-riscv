# RISC-V PCI xHCI keyboard and mouse gate

This gate proves one narrow input path on QEMU `virt`:

```text
PCI qemu-xhci -> DT-routed INTx -> CrabUSB -> USB HID boot keyboard + mouse
    -> shared Asterinas input worker -> evdev -> guest /init
```

The accepted configuration is RISC-V Sv39 with `smp=4`, one Red Hat
`1b36:000d` PCI xHCI controller, one QEMU `0627:0001` USB keyboard, and one
QEMU `0627:0001` USB mouse.
The QEMU device has MSI and MSI-X disabled explicitly. The gate does not allow a
VirtIO or i8042 keyboard fallback and starts QEMU with `-nic none`.

## Prerequisites

Run the commands from the repository root in the Asterinas development
container. In addition to the normal repository toolchain, the guest builder
needs `cpio` and the RISC-V libc and Linux UAPI headers:

```bash
apt-get install -y --no-install-recommends \
  libc6-dev-riscv64-cross linux-libc-dev-riscv64-cross cpio
```

The validated run used
`asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached` and QEMU 10.2.1.
The standard `asterinas/asterinas:0.18.0-20260702` image is also suitable after
installing the RISC-V cross compiler, `dtc`, and the U-Boot build dependencies
listed in the top-level RISC-V guide.

## Unit gate

Run the host, builder, guest-state-machine, lifecycle, and orchestration tests:

```bash
make test_riscv_xhci_input_unit
```

The builder creates a deterministic raw `newc` archive containing only `.` and
an executable static RISC-V `/init`. The guest accepts exactly one `BUS_USB`
keyboard named `usb_boot_keyboard` and one `BUS_USB` relative pointer named
`usb_boot_mouse`. It waits for delayed device registration, then requires this
exact event sequence:

1. `KEY_A` press and `SYN_REPORT`;
2. `KEY_A` release and `SYN_REPORT`;
3. `KEY_1` press and `SYN_REPORT`;
4. `KEY_1` release and `SYN_REPORT`.
5. `REL_X=17`, `REL_Y=-9`, and `SYN_REPORT`;
6. `BTN_LEFT` press/release, each followed by `SYN_REPORT`.

## Build and prepare

Build the gate initramfs and an Sv39 kernel. The Sv39 feature is mandatory:
QEMU is started with Sv48 disabled, so a default Sv48 kernel is not a valid
input to this gate.

```bash
mkdir -p target/riscv-xhci-input
tools/riscv/xhci/build_input_gate.sh \
  target/riscv-xhci-input/initramfs.cpio
make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
```

Prepare a private four-hart U-Boot disk. This path is intentionally separate
from `target/qemu-uboot/current` and other desktop/LTP artifacts.

```bash
ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
ASTERINAS_INITRAMFS="$PWD/target/riscv-xhci-input/initramfs.cpio" \
QEMU_UBOOT_PROFILE=generic-sv39-ltp-smp4 \
QEMU_UBOOT_OUT_DIR="$PWD/target/qemu-uboot/xhci-input" \
QEMU_UBOOT_BUILD_DIR="$PWD/target/qemu-uboot/cache/u-boot-build" \
  tools/riscv/prepare_qemu_uboot_booti.sh prepare
```

The host gate verifies that the prepared DTB has exactly four enabled CPU
nodes and that the disk payload matches `artifacts.json` before QEMU starts.

## Run

```bash
timeout 180s python3 tools/riscv/xhci/input_gate.py \
  --uboot target/qemu-uboot/cache/u-boot-build/u-boot \
  --boot-disk target/qemu-uboot/xhci-input/boot.ext4 \
  --manifest target/qemu-uboot/xhci-input/artifacts.json \
  --serial-log target/riscv-xhci-input/serial.log \
  --result target/riscv-xhci-input/result.json \
  --smp 4
```

Success requires all of the following current-run evidence, in order:

- PCI `0000:00:01.0 1b36:000d`, DT interrupt parent 9, interrupt 33;
- USB `0627:0001`, bus `usb`, name `usb_boot_keyboard`;
- USB `0627:0001`, bus `usb`, name `usb_boot_mouse`;
- both guest READY records, eight exact keyboard records and PASS, then seven
  exact pointer records and PASS;
- no panic marker, no alternative input device, and complete process-group
  cleanup.

The gate snapshots all inputs without following symlinks, bounds serial and HMP
operations, and atomically replaces stale evidence. Treat `result.json` as the
machine-readable verdict; a boot log alone is insufficient.

## Verified M1 evidence

The 2026-08-24 QEMU 10.2.1 run completed with `passed: true`, `smp: 4`, and
`cleanup: complete`. Its hashes were:

| Item | SHA-256 |
| --- | --- |
| Kernel Image | `8ab7647295e6bfa25a671871feb782f9f2a964d7c4df7705c9c66ecfdf627fb5` |
| DTB | `c9ef7d7b2bb81003e70f577588719d293d2b43e94e329b78ff176e21845ed148` |
| Input initramfs | `9fcc82709ac613e6adc41d31c9c9b2484d48837b588b5dbbee25a6523a519058` |
| Serial log | `eb466ad61c0a69eff3296c140b18541a3819c548c227691dc3b25e73acdbd87b` |
| Result JSON | `907a9dd544f5c1ff59efed78982ef69287cc7a45be57641d04f92571ea09744b` |

The ignored evidence files remain under `target/riscv-xhci-input/` in the
worktree that performed the run.

## Scope boundary

The current gate proves cold-boot enumeration and deterministic key and pointer
delivery for one QEMU PCI xHCI controller, one HID boot keyboard, and one HID
boot mouse. It does **not** prove USB hotplug, hubs, multiple devices per kind,
HID report protocol, key repeat, keyboard LEDs, physical Megrez xHCI,
MSI/MSI-X, or arbitrary xHCI hardware.

The physical Megrez path is deliberately a separate gate. A 2026-08-26 run
selected both board DWC3 controllers from a two-string
`/chosen/asterinas,usb-host` property: USB0 registered the keyboard and USB1
registered a mouse behind the board's VIA hub. Both interrupt workers reached
the Debian Desktop M3 Xorg session. See
[`2026-08-26-megrez-dual-xhci-desktop.md`](../../../docs/porting/evidence/2026-08-26-megrez-dual-xhci-desktop.md)
for the frozen topology and remaining human-interaction boundary.
