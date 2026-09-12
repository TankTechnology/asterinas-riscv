# RISC-V firmware boot validation

This directory validates the firmware-to-kernel handoff without modeling a single development board in QEMU.
The same runner describes a machine contract, a boot flow, and the observable milestones expected from the guest.
The registered profiles currently cover QEMU `virt`, SiFive U, Sv39, and the Sv48/Svade/Svadu envelope used by the Megrez integration.

The checks are evidence, not hardware emulation.
A passing profile proves the declared CPU, MMU, DTB, U-Boot `booti`, and userspace contracts.
It does not claim that QEMU reproduces unmodeled clocks, resets, cache controllers, or board peripherals.

## SMP4 cross-hart instruction-cache regression

The formal RISC-V regression job runs with exactly four guest CPUs and sets
`RISCV_ICACHE_REQUIRE_SMP4=1`. The guest test pins its writer to one CPU, warms
and rewrites executable code on each of the other three CPUs for 1024
generations, and rejects any topology other than four available CPUs.

The host validates the complete `qemu.log`, not only its tail. A pass requires
one overall regression terminal marker, one exact four-distinct-CPU icache
marker, no skip, and no panic, unexpected exception, or failed SBI remote
`fence.i` marker anywhere in the transcript:

```bash
make run_kernel AUTO_TEST=regression TARGET_ARCH=riscv64 SMP=4 \
  RISCV_ICACHE_REQUIRE_SMP4=1
```

This QEMU test validates the syscall and cross-hart software protocol. Physical
instruction-cache behavior still requires a result from the exact board binary
and configuration being claimed.

## Unit tests

Run the repository-contract tests before launching QEMU:

```bash
make test_riscv_uboot_booti_unit
```

These tests validate immutable profile definitions, address ranges, generated commands, DTB policy, milestone accounting, result classification, cleanup, and artifact identity checks.

## Linux Test Project syscall gate

The isolated LTP gate cross-builds the pinned LTP `20260529` syscall suite,
boots it through the guarded U-Boot runner,
and stores its evidence below `target/ltp/`.
It does not reuse `target/qemu-uboot/current`,
so running it cannot replace the prepared desktop boot disk.

Run its host tests with:

```bash
make test_riscv_ltp_unit
```

After building the RISC-V kernel and LTP initramfs,
record a baseline with:

```bash
python3 tools/riscv/ltp_gate.py run \
  --kernel target/osdk/aster-kernel-osdk-bin.Image \
  --smp 1 --run-id baseline-m1-smp1 --skip-build --baseline
```

See [the LTP gate operator guide](ltp/README.md)
for the exact containers, strict mode, SMP=4 runs, result schema,
and evidence-provenance rules.

## PCI xHCI USB keyboard gate

The PCI xHCI gate boots QEMU `virt` in Sv39 mode with `smp=4`, one PCI
`qemu-xhci` controller, and one USB HID boot keyboard. It proves the
DT-routed INTx, xHCI, USB enumeration, input-core, and evdev path with an exact
press/release sequence and no VirtIO or i8042 keyboard fallback.

Run its host tests with:

```bash
make test_riscv_xhci_input_unit
```

See [the PCI xHCI keyboard operator guide](xhci/README.md) for the Sv39 build,
private U-Boot disk, bounded QEMU command, evidence schema, verified M1 hashes,
and the physical-board limitations.

## Debian persistent root

The Debian M1 gate validates a signed Trixie `riscv64` ext2 root, hands off
from a minimal stage-1 initramfs into Debian `/bin/bash`, and boots the same
writable root twice to prove persistence. It is an infrastructure gate: four
harts, Sv39, two VirtIO block devices, no network, display, USB, or input.

Run its local contract tests with:

```bash
make test_riscv_debian_rootfs_unit
```

See [the Debian persistent-root operator guide](debian/rootfs/README.md) for
the signed root build, current-main kernel/U-Boot/DTB/stage-1 preparation,
explicit two-boot target, and evidence inspection commands.

## Megrez persistent Debian shell

This workflow freezes separate current kernels for the two MMU contracts:
generic QEMU is built with `FEATURES=riscv_sv39_mode`, while Megrez uses the
default Sv48 build. Never reuse one artifact for the other platform. The
bundle also binds the signed Debian root, Stage1, both four-hart DTBs, and
U-Boot to one clean Git commit.

The physical order is deliberately read-only first: run ` inventory ` before
` install-if-needed `. A matching inventory skips installation. A measured
mismatch may authorize the Asterinas-only installer to write exactly
`/dev/mmcblk0p2`; this workflow must not boot Linux as an installer or runtime.
The short EIC7700X watchdog is forbidden during a full-device hash or install;
the bounded Asterinas reboot timer and fresh U-Boot recovery epoch are used
instead. `gate` performs two boots with one persistence nonce, and `handoff`
is allowed only after that physical result passes.

The physical boot transport is serial YMODEM for the compressed current
kernel and Stage1, plus a read-only, CRC-checked load of
`eic7700-milkv-megrez.dtb` from eMMC partition 1. The board's U-Boot GMAC at
`0x50400000` is not the RJ45 path used by Asterinas, so TFTP is deliberately
not used for inventory, the two-boot gate, or handoff. The network installer
starts only after Asterinas owns its verified RJ45 GMAC.

Use one stable evidence directory:

```bash
RUN="$PWD/target/megrez-debian-shell/$(git rev-parse --short=12 HEAD)"
python3 -m tools.riscv.megrez_debian_shell check "$RUN/plan.json"
sudo -E python3 -m tools.riscv.megrez_debian_shell qemu \
  "$RUN/plan.json" --output "$RUN/qemu"
python3 -m tools.riscv.megrez_debian_shell permit \
  "$RUN/plan.json" --qemu-evidence "$RUN/qemu/qemu-evidence.json" \
  --output "$RUN/permit.json"
sudo -E python3 -m tools.riscv.megrez_debian_shell inventory \
  "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
  --output "$RUN/inventory-before" --yes
sudo -E python3 -m tools.riscv.megrez_debian_shell install-if-needed \
  "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
  --inventory "$RUN/inventory-before/result.json" --output "$RUN/install" --yes
if jq -e '.status == "needs-install"' "$RUN/inventory-before/result.json"; then
  sudo -E python3 -m tools.riscv.megrez_debian_shell inventory \
    "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
    --prior-inventory "$RUN/inventory-before/result.json" \
    --install-result "$RUN/install/result.json" \
    --output "$RUN/inventory-after" --yes
  cp "$RUN/inventory-after/result.json" "$RUN/inventory-current.json"
else
  cp "$RUN/inventory-before/result.json" "$RUN/inventory-current.json"
fi
sudo -E python3 -m tools.riscv.megrez_debian_shell gate \
  "$RUN/plan.json" /dev/ttyUSB0 --permit "$RUN/permit.json" \
  --inventory "$RUN/inventory-current.json" --output "$RUN/physical" \
  --host-interface enp12s0 --yes
sudo -E python3 -m tools.riscv.megrez_debian_shell handoff \
  "$RUN/plan.json" /dev/ttyUSB0 --result "$RUN/physical/result.json" \
  --host-interface enp12s0 --yes
picocom --baud 115200 --flow n --parity n --databits 8 /dev/ttyUSB0
```

This milestone proves a persistent interactive Debian shell. The next scope is
systemd, network, and desktop; none is claimed by this gate.

## VirtIO-GPU hardware cursor gate

The DRM R1 gate boots current-main Asterinas with the generic Sv39, SMP=4
profile and one `virtio-gpu-device`. Its guest performs a 64x64 Cursor2 set,
legacy cursor move, and cursor hide. A pass requires the guest markers and the
QEMU VirtIO cursor traces in the exact order; networking, USB, and input-device
fallbacks are absent.

Run the host contract tests with:

```bash
make test_riscv_drm_cursor_unit
```

Build the dedicated initramfs and a matching Sv39 kernel, then prepare a
private U-Boot disk:

```bash
tools/riscv/drm/build_cursor_gate.sh \
  target/qemu-uboot/drm-cursor/initramfs.cpio.gz
make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode

ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
ASTERINAS_INITRAMFS="$PWD/target/qemu-uboot/drm-cursor/initramfs.cpio.gz" \
QEMU_UBOOT_PROFILE=generic-sv39-drm-cursor-smp4 \
QEMU_UBOOT_OUT_DIR="$PWD/target/qemu-uboot/drm-cursor/prepared" \
QEMU_UBOOT_BUILD_DIR="$PWD/target/qemu-uboot/cache/u-boot-build" \
tools/riscv/prepare_qemu_uboot_booti.sh prepare
```

Run the bounded evidence gate:

```bash
make test_riscv_drm_cursor \
  DRM_CURSOR_UBOOT="$PWD/target/qemu-uboot/cache/u-boot-build/u-boot" \
  DRM_CURSOR_BOOT_DISK="$PWD/target/qemu-uboot/drm-cursor/prepared/boot.ext4" \
  DRM_CURSOR_MANIFEST="$PWD/target/qemu-uboot/drm-cursor/prepared/artifacts.json" \
  DRM_CURSOR_GATE_OUTPUT="$PWD/target/qemu-uboot/drm-cursor/evidence"
```

This gate proves the current-main VirtIO transport and DRM cursor ioctl path;
it is not evidence for the Megrez display controller or physical scanout.

## Simulation-first Megrez debug attempt

The Megrez debug workflow binds one immutable Asterinas artifact plan to a
generic Sv39/SMP=4 QEMU fast result before touching the board. The physical
step holds one serial descriptor, verifies the kernel, stage-1 initramfs, and
Megrez DTB already in RAM by exact CRC32, and transfers only cache misses over
XMODEM. It patches the live DTB for the firmware framebuffer and USB host,
runs exactly one `booti`, and waits for the plan's guest marker followed by
the automatic U-Boot recovery prompt.

It never transfers the 1-GiB Debian root image, runs `saveenv`, resets the
board, or boots Linux. A failed or interrupted attempt publishes
`passed:false`; `result.json` is written after the serial and transport
evidence.

```bash
make test_riscv_megrez_debug_board \
  MEGREZ_DEBUG_PLAN="$PWD/target/megrez-debug/debug-plan.json" \
  MEGREZ_DEBUG_DEVICE=/dev/ttyUSB0 \
  MEGREZ_DEBUG_SIMULATION_RESULT="$PWD/target/qemu-uboot/megrez-debug/fast/result.json"
```

The command has one declining timeout, capped at 900 seconds. Its default
remains 300 seconds. A desktop plan using `asterinas.reboot_after=600` must use
`--timeout 900`: the Megrez guest clock can advance more slowly than host
monotonic time, so a 660-second host budget can expire before the bounded guest
recovery. Reusing RAM is safe only when U-Boot reports the exact planned
size/address CRC; otherwise the artifact is retransmitted and verified again
before `booti`.

For a diagnostic boot that may hang before Asterinas can arm
`asterinas.reboot_after`, add `--hardware-watchdog`. This is an explicit
pre-boot recovery option, not the desktop default:

```bash
PYTHONPATH="$PWD" python3 -m tools.riscv.megrez_debug board \
  /absolute/path/to/plan.json /dev/ttyUSB0 \
  --simulation-result /absolute/path/to/fast/result.json \
  --output-directory /absolute/path/to/physical-evidence \
  --timeout 120 \
  --hardware-watchdog
```

The option follows the EIC7700X TRM's Synopsys DesignWare watchdog contract at
`0x50800000`. Before touching that block, it reads the system-controller clock
gate at `0x51828200` and active-low reset at `0x51828444`, preserves unrelated
bits, deasserts only WDT0 reset when required, and verifies both values again.
It then verifies component type `0x44570120`, selects maximum `TOP=0xf`
(`TORR[7:4]` is reserved on EIC7700X), kicks with `0x76`, enables
interrupt-then-reset mode, and reads the control registers back. Any
prerequisite, type, or readback mismatch aborts before the kernel starts. It
writes neither storage nor U-Boot environment. A watchdog recovery that occurs
after the first current-guest marker but before the terminal marker is reported
immediately as `guest-reboot-before-terminal`; a bare pre-boot prompt is not
mistaken for current-attempt evidence. The host retains its independent
300-second cap even though the current DT describes a 200 MHz watchdog clock.

## Current-main Megrez physical graphics

This gate is based on `origin/main` commit
`69a7b6e41ca74932f79d917f3638199da573b1e9`. The candidate branch adds the
opt-in debug-root console and physical-interaction witness without importing a
different RISC-V kernel baseline. Record both `git rev-parse origin/main` and
`git rev-parse HEAD` with every run so that the upstream base and candidate
source remain distinguishable.

Before building, require a clean tracked worktree and confirm that the pinned
base is an ancestor. The QEMU and physical results record artifact SHA-256
values; retain this source record beside those results:

```bash
set -euo pipefail
PINNED_MAIN=69a7b6e41ca74932f79d917f3638199da573b1e9
EVIDENCE_ROOT="$PWD/target/current-main-physical-graphics"
SOURCE_RECORD="$EVIDENCE_ROOT/source-identity.txt"
mkdir -p -m 0700 "$EVIDENCE_ROOT"
test ! -e "$SOURCE_RECORD"
test -z "$(git status --porcelain --untracked-files=all)"
test "$(git rev-parse origin/main)" = "$PINNED_MAIN"
git merge-base --is-ancestor "$PINNED_MAIN" HEAD
SOURCE_TMP=$(mktemp "$EVIDENCE_ROOT/.source-identity.XXXXXX")
trap 'rm -f -- "$SOURCE_TMP"' EXIT
{
  printf 'origin_main=%s\n' "$(git rev-parse origin/main)"
  printf 'candidate_head=%s\n' "$(git rev-parse HEAD)"
  printf 'tracked_and_untracked_clean=true\n'
} >"$SOURCE_TMP"
chmod 0600 "$SOURCE_TMP"
mv -- "$SOURCE_TMP" "$SOURCE_RECORD"
trap - EXIT
```

The QEMU run binds the source-built current-main `--kernel` to seven immutable
supporting inputs: `--uboot`, `--dtb`, `--stage1-initramfs`, `--root-image`,
`--root-manifest`, `--packages-lock`, and `--package-checksums`. Use one set of
these exact paths for the debug-console, browser-web, and interaction gates;
do not rebuild or replace an input between runs.

```bash
make test_riscv_physical_graphics_unit

QEMU_INPUTS=(
  "DEBIAN_KERNEL=$PWD/target/osdk/aster-kernel-osdk-bin.Image"
  "DEBIAN_UBOOT=$PWD/target/qemu-uboot/cache/u-boot-build/u-boot"
  "DEBIAN_DTB=$PWD/target/qemu-uboot/current/qemu-virt.dtb"
  "DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-riscv/stage1/initramfs.cpio"
  "DEBIAN_ROOT_IMAGE=$PWD/target/debian-riscv/browser-web/rootfs/debian-root.ext2"
  "DEBIAN_ROOT_MANIFEST=$PWD/target/debian-riscv/browser-web/rootfs/rootfs-manifest.json"
  "DEBIAN_PACKAGES_LOCK=$PWD/target/debian-riscv/browser-web/rootfs/packages.lock"
  "DEBIAN_PACKAGE_CHECKSUMS=$PWD/target/debian-riscv/browser-web/rootfs/source-metadata/package-checksums"
)
make test_riscv_debian_debug_console_qemu_gate "${QEMU_INPUTS[@]}" \
  DEBIAN_DEBUG_CONSOLE_QEMU_GATE_OUTPUT="$PWD/target/current-main-physical-graphics/qemu-debug-console"
make test_riscv_debian_browser_web_qemu_gate "${QEMU_INPUTS[@]}" \
  DEBIAN_BROWSER_WEB_QEMU_GATE_OUTPUT="$PWD/target/current-main-physical-graphics/qemu-browser-web"
make test_riscv_physical_graphics_qemu_gate "${QEMU_INPUTS[@]}" \
  RISCV_PHYSICAL_GRAPHICS_QEMU_GATE_OUTPUT="$PWD/target/current-main-physical-graphics/qemu-interaction"
```

The QEMU adapter injects keys through HMP and absolute pointer events through
a private QMP socket into the VirtIO keyboard and tablet.
HMP relative mouse moves cannot drive the tablet.
It validates three nonce-bound browser cycles and captures pixels, but its
result always records `"physical":false`: QEMU cannot satisfy the physical
result or prove the Megrez display scanout and real USB xHCI/HID paths.

The physical guest keeps its fixed 900-second safety reboot. The gate defaults
to three cycles for release-grade repeatability, but accepts `--cycles 1` for
one information-rich experimental interaction. Graphical setup, the requested
cycles, display evidence, and final verification share that lifetime, with 30
seconds reserved before reboot.
Per-phase timeout settings are upper bounds, not extensions of the board's
remaining lifetime; an exhausted budget fails closed and proceeds to recovery.

The real run consumes a schema-2 `debian-browser` debug plan. Its canonical
order is `kernel`, `initramfs`, `qemu_dtb`, `megrez_dtb`, `u_boot`,
`root_image`, `root_manifest`, `packages_lock`, `package_checksums`, and
`in_release`; the prepare target recomputes every size, SHA-256, and CRC32.
Use a stable `/dev/serial/by-id/...` path. The release-grade mode asks an HDMI
capture program to atomically create or replace the `--hdmi-capture` file only
after the gate asks for the cyan final-cycle image. First validate every plan
artifact and print the exact command, without opening the serial device or
changing U-Boot state:

```bash
make prepare_riscv_megrez_physical_graphics \
  MEGREZ_PHYSICAL_GRAPHICS_PLAN="$PWD/target/megrez-debug/debug-plan.json" \
  MEGREZ_PHYSICAL_GRAPHICS_DEVICE=/dev/serial/by-id/usb-REPLACE_ME \
  MEGREZ_PHYSICAL_GRAPHICS_HDMI_CAPTURE="$PWD/target/current-main-physical-graphics/physical/operator-hdmi.png" \
  MEGREZ_PHYSICAL_GRAPHICS_OUTPUT="$PWD/target/current-main-physical-graphics/physical/evidence"
```

Run the command printed by that target. For each of the three 180-second
interaction windows, type the displayed random 16-hex-digit nonce on the
physical USB keyboard, then move the physical USB mouse and click the amber
button. The guest is booted with `asterinas.reboot_after=900`; the host uses a
930-second recovery wait so it can retain the final HDMI image and still
observe the fresh U-Boot prompt. The printed command makes every deadline
explicit: opening the serial link is bounded at 60 seconds, artifact
preparation at 300 seconds, graphical readiness at 300 seconds, each cycle at
180 seconds, and the post-cycle HDMI update at 60 seconds. The post-boot
success-path caps total 720 seconds, leaving 180 seconds of the guest's single
900-second lifetime for serial transfer and phase transitions. These are caps,
not reserved waiting periods; respond to each prompt immediately. The capture
must be a complete 1-byte-to-64-MiB PNG or JPEG that remains unchanged for at
least 0.5 seconds. A missing input record, DOM transition,
screenshot, HDMI update, recovery prompt, or any panic/xHCI/framebuffer fatal
marker produces `passed:false` while retaining the diagnostic evidence.

When external capture hardware is unavailable during experimental development,
select the explicitly weaker `operator-attested` mode. It preserves real USB
evdev input, trusted Firefox DOM state, the serial guest PNG, final Firefox PID
and restart checks, and recovery, but it does not claim that an HDMI image was
captured. Existing versioned MMC files can be selected so this path performs no
artifact transfer or image rebuild:

```bash
make prepare_riscv_megrez_physical_graphics \
  MEGREZ_PHYSICAL_GRAPHICS_PLAN="$PWD/target/current-main-physical-graphics/physical/plan-firefox-77d7e42c.json" \
  MEGREZ_PHYSICAL_GRAPHICS_DEVICE=/dev/serial/by-id/usb-REPLACE_ME \
  MEGREZ_PHYSICAL_GRAPHICS_DISPLAY=operator-attested \
  MEGREZ_PHYSICAL_GRAPHICS_CYCLES=1 \
  MEGREZ_PHYSICAL_GRAPHICS_MMC_KERNEL=asterinas-77d7e42c-220572e8.Image \
  MEGREZ_PHYSICAL_GRAPHICS_MMC_INITRAMFS=asterinas-78c4a36c-f4d9b349-stage1.cpio \
  MEGREZ_PHYSICAL_GRAPHICS_MMC_DTB=dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb \
  MEGREZ_PHYSICAL_GRAPHICS_OUTPUT="$PWD/target/current-main-physical-graphics/physical/operator-attested-evidence"
```

After the single keyboard/mouse cycle reaches cyan PASS, visually inspect the
physical monitor and enter exactly the printed nonce-bound line,
`confirm-cyan-pass <suffix>`. A successful one-cycle result
proves one complete interaction path. Such a result does not prove three-cycle repeatability.
The evidence is published as `operator-display-attestation.json`, with `hdmi: null`; no file is
presented as an HDMI capture.

## Megrez fast kernel probes

Use the fast probe path for routine kernel work that does not need Debian,
systemd, Firefox, network access, or partition 2.
Build and deploy a versioned kernel, Stage1 initramfs, and DTB only when their
identity changes, then select them once with `configure`.
Routine runs use only the immutable bundle and the files already on MMC
partition 1:

```bash
python3 -m tools.riscv.megrez_probe boot syscall213
```

The default bundle is `target/megrez-probe/current.json`; private evidence is
written below `target/megrez-probe/latest` by default.
The guest has one 90-second timer, recovery is independently bounded, and the
host sends one newline after the new U-Boot banner to stop its autoboot
countdown before requiring the prompt.
The normal probe registry is fixed and read-only.
A bounded shell is available only when explicitly requested for a physical
diagnostic run.

Run the host and Stage1 regression tests in the persistent container:

```bash
tools/docker/run_dev_container.sh -- make test_riscv_megrez_probe_unit
```

The same lifecycle has two QEMU gates: one executes `boot syscall213`, and one
sends no request so the Stage1 kernel timer must reboot the guest. Override the
three artifact variables only when testing a non-default build:

```bash
tools/docker/run_dev_container.sh -- make test_riscv_megrez_probe_qemu \
  MEGREZ_PROBE_BUNDLE=target/megrez-probe/current.json \
  MEGREZ_PROBE_KERNEL=target/osdk/aster-kernel-osdk-bin.Image \
  MEGREZ_PROBE_INITRAMFS=target/megrez-probe/build/initramfs.cpio
```

Deployment is a separate maintenance operation.  Every kernel, Stage1, and
DTB basename must contain the first 12 hexadecimal digits of that artifact's
SHA-256.  The extlinux label contains the first 12 digits of the plan SHA-256.
Keep the generated configuration at
`<staged-directory>/extlinux/asterinas.conf` and validate the complete staged
tree before opening the serial port.

Render that configuration atomically from the frozen plan and the three
partition-relative paths:

```bash
python3 -m tools.riscv.megrez_boot_manifest render \
  --plan "$PWD/target/megrez-debug/current/plan.json" \
  --mmc-kernel /asterinas-KERNEL_SHA12.booti \
  --mmc-initramfs /stage1-INITRAMFS_SHA12.cpio \
  --mmc-dtb /dtb/megrez-DTB_SHA12.dtb \
  --output "$PWD/target/megrez-boot-manifest/current/extlinux/asterinas.conf"
```

With that directory already served read-only on the private board network,
publish it once through RockOS:

```bash
python3 -m tools.riscv.megrez_rockos_attestation publish \
  /dev/serial/by-id/usb-FTDI_FT232R_USB_UART-REPLACE-if00-port0 \
  --plan "$PWD/target/megrez-debug/current/plan.json" \
  --extlinux-config "$PWD/target/megrez-boot-manifest/current/extlinux/asterinas.conf" \
  --staged-directory "$PWD/target/megrez-boot-manifest/current" \
  --base-url http://10.100.19.216:18081/current \
  --output-directory "$PWD/target/current-main-physical-graphics/physical/publication"
```

The command checks that `/boot` is `/dev/mmcblk1p1`, verifies or installs only
new immutable artifact names, and replaces `/boot/extlinux/asterinas.conf`
atomically last.  A failure leaves the previous configuration in place and
still attempts a normal reboot to U-Boot.  It never accesses partition 2.

After the RockOS receipt succeeds, select exactly those persistent bytes once:

```bash
python3 -m tools.riscv.megrez_probe configure \
  --plan "$PWD/target/megrez-debug/current/plan.json" \
  --device /dev/serial/by-id/usb-FTDI_FT232R_USB_UART-REPLACE-if00-port0 \
  --mmc-kernel asterinas-KERNEL_SHA12.booti \
  --mmc-initramfs stage1-INITRAMFS_SHA12.cpio \
  --mmc-dtb dtb/megrez-DTB_SHA12.dtb \
  --extlinux-config "$PWD/target/megrez-boot-manifest/current/extlinux/asterinas.conf" \
  --mmc-extlinux extlinux/asterinas.conf
```

The probe bundle is schema 2 and binds the extlinux contents and its
size/SHA-256/CRC32 in addition to the three artifacts.  Each physical probe
loads and CRC-checks the persistent extlinux file first; a mismatch stops
before `booti`.  Schema 1 remains accepted only by the explicit diskless QEMU
adapter.

This prevents `MEGREZ-BOOT-MANIFEST-001`, where the reset entry referenced the
missing `asterinas-sv48-fe1dcfdf7.booti` and
`initramfs-full-712208ba4.cpio`.  The host-side fixture reports
`kernel: staged file is missing`; a physical MMC mismatch reports an extlinux
size/CRC failure and does not try another kernel.

The schema-2 RockOS attestation below remains the full Debian/browser release
workflow.  The lightweight probe does not start RockOS, Firefox, network, or
partition 2 during routine runs.
A firmware or
SBI hard lock that prevents all serial progress still requires a manual board
reset.

### Review policy for the experimental RISC-V fork

This RISC-V support is experimental development in the `asterinas-riscv` fork.
The fork intentionally does not carry or invoke the former repository-local
`aster-code-review` skill, its automation, or its compatibility symlink.
Use normal human-readable diff review plus the existing tests and hardware
gates instead.
When integrating future upstream changes, keep this removal as an explicit
fork policy unless the project owner decides to adopt a replacement review
workflow.

## Megrez unattended boot stability

The unattended gate separates deployment from acceptance. The schema-2
`DebugPlan` is the immutable deployment manifest; its sizes, SHA-256 values,
CRC32 values, and versioned MMC paths identify one release. A routine gate run
loads only the existing kernel, Stage1 initramfs, and DTB from MMC partition 1.
It does not build, upload, or fall back to serial transfer;
partition 2 is never written.

Keep compilation and all unit tests in the persistent development container:

```bash
tools/docker/run_dev_container.sh -- \
  make test_riscv_megrez_boot_stability_unit
```

Boot RockOS only when the next `DebugPlan` names a kernel or initramfs that is
not already present on partition 1.
Transfer only changed, versioned files,
then verify them with the controlled measurement tool below. It boots RockOS,
uses native `stat` and `sha256sum`, binds every output to a fresh random nonce
and the plan identity, performs a normal reboot, and requires a new
OpenSBI/U-Boot epoch before publishing. The private raw serial source is kept
as `deployment-measurement.serial.log`; the derived schema-2
`deployment-attestation.json` binds its SHA-256, RockOS boot ID,
`/dev/mmcblk1p1`, filenames, sizes, and observed SHA-256 values.

With the board at a fresh U-Boot prompt, generate the receipt once. The
password prompt is read from the terminal and is never placed in argv, an
environment variable, or the retained transcript:

```bash
python3 -m tools.riscv.megrez_rockos_attestation \
  /dev/serial/by-id/usb-FTDI_FT232R_USB_UART-REPLACE-if00-port0 \
  --plan "$PWD/target/current-main-physical-graphics/physical/plan-isolated-resolved.json" \
  --output-directory "$PWD/target/current-main-physical-graphics/physical/rockos-attestation" \
  --mmc-kernel asterinas-COMMIT-CRC.Image \
  --mmc-initramfs asterinas-COMMIT-CRC-stage1.cpio \
  --mmc-dtb dtbs/linux-image-VERSION/eswin/eic7700-milkv-megrez.dtb
```

The receipt and raw log are reused by every later boot of the same immutable
deployment. Retain the preceding plan, files, receipt, and log for rollback. A
kernel update does not require rebuilding or reinstalling the partition-2
Debian root. This is an auditable maintenance receipt, not a hardware root of
trust: the host controlling the exclusive serial port, this tool, and RockOS
remain inside the deployment trust boundary.

Run the gate directly on the host as the `dialout` user. The routine gate
validates the frozen plan and RockOS SHA-256 receipt, then observes the MMC
size/CRC32 at U-Boot. It does not reread local build outputs or require the
plan's original container paths to exist.
This keeps serial ownership and the private evidence files with the host user.
With the board at a fresh U-Boot prompt, run three unattended Asterinas boot
and recovery epochs using the frozen plan and the filenames already verified
on MMC:

```bash
python3 -m tools.riscv.megrez_boot_stability \
  /dev/serial/by-id/usb-FTDI_FT232R_USB_UART-REPLACE-if00-port0 \
  --plan "$PWD/target/current-main-physical-graphics/physical/plan-isolated-resolved.json" \
  --output-directory "$PWD/target/current-main-physical-graphics/physical/boot-stability" \
  --deployment-attestation "$PWD/target/current-main-physical-graphics/physical/rockos-attestation/deployment-attestation.json" \
  --deployment-measurement-log "$PWD/target/current-main-physical-graphics/physical/rockos-attestation/deployment-measurement.serial.log" \
  --mmc-kernel asterinas-COMMIT-CRC.Image \
  --mmc-initramfs asterinas-COMMIT-CRC-stage1.cpio \
  --mmc-dtb dtbs/linux-image-VERSION/eswin/eic7700-milkv-megrez.dtb
```

Each cycle verifies the MMC byte count and CRC32, reaches the isolated root
debug console, masks the external network-evidence workload, and bind-mounts
the volatile `/run/asterinas-physical-home` over `/home/asterinas`. Xorg,
Openbox, and Firefox therefore keep logs, profiles, and caches on tmpfs instead
of relying on unsupported partition-2 writeback. The gate then requires
systemd, `/dev/fb0`, Xorg using that framebuffer, Openbox, and an active Firefox
service with zero restarts. It has no keyboard, mouse, HDMI, or network-success
requirement.

Serial shell work is split into short, idempotent, acknowledged steps. A lost
acknowledgement is detected after 15 seconds; the host sends Control-C and a
newline to restore the shell boundary before retrying. Before an immediate
`reboot -f`, the gate retains a bounded snapshot of the kernel ring buffer,
mounts, failed systemd units, graphical unit state, process tree, full service
status, journal, and Xorg/Firefox logs. An oversized diagnostic snapshot fails
closed instead of dropping the earlier kernel log. `cycle-1.diagnostics.log`
and its peers contain those snapshots; `deployment-measurement.serial.log`,
`deployment-attestation.json`, `deployment.json`, `sha256sums.txt`, and the
per-cycle serial logs bind them to the release.

`result.json` is replaced last and reports pass only after all three cycles
return to distinct fresh U-Boot epochs without a panic, oops, fatal exception,
out-of-memory event, ext2 error, or block I/O error. To roll back, select the
previous plan and its retained versioned filenames, then run the same gate.

The default 240-second readiness deadline covers the measured cold-start
variance; it is an upper bound and does not delay a successful cycle. Remaining
`systemd-random-seed`/`systemd-sysctl` failures, read-only block writeback
attempts, syscalls 213/272, and `SA_NOCLDSTOP` warnings are retained as kernel
compatibility work. They are not hidden by the deployment gate and are not
treated as proof of a fatal graphics failure.

## One-command Megrez desktop and Firefox diagnosis

Configure the already deployed, measured MMC release once. This operation is
local-only: it validates and hashes the existing plan, RockOS receipt, and
measurement log, then writes one private bundle. It does not open the serial
device, build an image, transfer a file, boot RockOS, or write either MMC
partition:

The bundle records the plan's canonical semantic SHA-256 identity. The receipt
and measurement-log fields record their exact byte SHA-256 identities; loading
the bundle cross-validates all three before opening the serial device.

```bash
python3 -m tools.riscv.megrez_desktop configure \
  --plan "$PWD/target/current-main-physical-graphics/physical/plan-isolated-resolved.json" \
  --device /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0 \
  --deployment-attestation "$PWD/target/current-main-physical-graphics/physical/rockos-attestation-isolated-resolved/deployment-attestation.json" \
  --deployment-measurement-log "$PWD/target/current-main-physical-graphics/physical/rockos-attestation-isolated-resolved/deployment-measurement.serial.log" \
  --mmc-kernel asterinas-790ab694-34bc1cc0.Image \
  --mmc-initramfs asterinas-78c4a36c-f4d9b349-stage1.cpio \
  --mmc-dtb dtbs/linux-image-6.6.87-win2030/eswin/eic7700-milkv-megrez.dtb \
  --evidence-root "$PWD/target/megrez-desktop/evidence" \
  --output "$PWD/target/megrez-desktop/current.json"
```

After that one-time configuration, the routine desktop boot is one command:

```bash
python3 -m tools.riscv.megrez_desktop start
```

`start` reads only the three named files already on MMC partition 1, verifies
their U-Boot byte counts and CRC32 values, and reaches the existing systemd,
framebuffer, Xorg, Openbox, and Firefox readiness contract. A successful run
closes the host serial descriptor and leaves the desktop running; it does not
arm the 900-second diagnostic reboot timer. A failure after Asterinas starts
collects bounded `dmesg`, systemd, process, Xorg, and Firefox evidence, requests
`reboot -f`, and checks for a new U-Boot epoch. Complete firmware/SBI loss is
reported as `manual-reset-required`; without an independent reset controller,
software cannot recover that state remotely.

For one unattended real-web check, use the same configured bundle:

```bash
python3 -m tools.riscv.megrez_desktop browse-firefox
```

`browse-firefox` owns a temporary host bridge from `10.100.19.216:17893` to
the local proxy (port `7890` by default), boots the existing three MMC files
once, and retains the frozen network and static-neighbor boot arguments. It
synchronizes the guest clock from a validated plain-HTTP `Date` header before
any HTTPS request, waits for Marionette with a real loopback TCP connection,
and uses one WebDriver session to validate and capture only the Baidu homepage.
It does not run the 20-request fixture stress test, rewrite partition 2, or use
`/proc/net/tcp` as a readiness oracle. The JSON and PNG are transferred with
nonce, byte-count, and SHA-256 framing, after which the action requests a reboot
and verifies a fresh U-Boot prompt. The PNG is a Marionette content-viewport
capture, not an HDMI framebuffer dump: it must be a complete decodable image of
at least 1024x700, while the paired JSON and ready marker independently bind the
same WebDriver session to Baidu HTTPS, verified TLS timing, and the required DOM.
The real-web action has a 1050-second terminal recovery timer and stops its page
gate early enough to export failure evidence. It records payload-free
Marionette command boundaries plus at most 128 kernel TCP events for loopback
port 2828; routine `start` keeps its existing non-diagnostic behavior.
Increasing the terminal bound does not delay a successful page. On a host with
a different local proxy port, pass `--proxy-upstream-port PORT`.

The two experiment helpers are carried by the small Stage1 initramfs and
bind-mounted into the ephemeral `/run/asterinas-tools` path. Updating this
workflow therefore replaces only the versioned Stage1 file on MMC partition 1
(about 650 KiB); deployment does not rewrite the Debian root image on
partition 2.

Use the heavier action only for one explicitly falsifiable Firefox experiment:

```bash
python3 -m tools.riscv.megrez_desktop diagnose-firefox \
  --hypothesis "The physical Firefox Marionette greeting completes and WebDriver:NewSession is fully sent but produces no response-header byte before the fixed 300-second deadline." \
  --contrary-outcome "The listener does not become ready, NewSession is not fully sent, a response is partial or rejected, or NewSession returns a valid session."
```

The diagnostic records a stable Firefox identity, captures the before snapshot,
then lets the one selected `WebDriver:NewSession` operation perform the loopback
connection, Marionette greeting, request, and response under one 300-second
absolute deadline. It does not send `WebDriver:Status` because that command is
not part of Firefox ESR 140's direct Marionette command table. Exact offline
NewSession error output is enabled only for this bounded diagnostic. It also
captures during/after Firefox-tree and syscall snapshots, kernel/service logs,
and payload-free Marionette transport counters, then always requests recovery.
The complete physical experiment is capped at 15 host minutes before the
independent recovery wait. It records zero QEMU runs, exactly one physical boot,
and requires zero artifact-transfer bytes.

Every runtime identity is admitted once in the mode-`0600`
`target/megrez-desktop/evidence/experiments.jsonl` ledger. Rewording the
hypothesis does not permit another identical boot. A repeat requires a proven
observer defect and a changed, regression-tested diagnostic protocol identity.
Each run gets a new mode-`0700` directory; raw evidence is mode `0600`, and
`result.json` is published last with hashes for every retained input and file.
The frozen partition-2 image is not reinstalled. Because
`/home/asterinas` is backed by the physical run's volatile tmpfs, Firefox still
starts with a cold profile on every boot; current measured graphical readiness
is approximately 175–220 seconds, not an instant warm resume.

Protocol v7 writes result schema version 2. Its classifier reports only the first
observed boundary: listener unavailable, NewSession not sent, response absent,
response partial, response rejected, response complete, or evidence incomplete.
Older `mmc-graphics-final-19` evidence predates transport records and therefore
classifies only as `evidence-incomplete`; it is not proof of a TCP, `poll`,
scheduler, or Firefox deadlock. If a new run is also incomplete, fix and replay
the observer before any further live Firefox boot.

Run the focused host tests in the persistent development container. The tests
use local loopback sockets, so do not pass the container launcher's `--offline`
network-isolation flag; the command still performs no dependency download:

```bash
tools/docker/run_dev_container.sh -- make test_riscv_megrez_desktop_unit
```

## Megrez SDHCI read-only evidence

The Megrez SDHCI gate classifies a bounded Asterinas serial transcript. It
requires an aligned 512 KiB SDMA buffer whose CPU and device addresses are
identical inside `0xc0000000..0x100000000`, the EIC7700 removable-card
controller, a nonzero SDHC capacity, and read-only `mmcblk0` registration in
that order. For the physical data-path gate it then requires one exact 32 MiB
read whose CRC32 matches the value measured by U-Boot. That read covers the
partition table and is stronger than the old, never-implemented
`partition-table sha256` log requirement. The identity address is the RockOS
U-Boot handoff contract; Linux's `0x20000000` IOVA requires SMMUv3 SID 16 and
is not usable as a fixed offset while Asterinas RISC-V has no IOMMU. Panic,
fatal, probe-failure, writable, translated, misaligned, duplicate, and
out-of-order evidence is rejected. Linux boot output is not an accepted
substitute.

Run the host tests with:

```bash
python3 -m unittest tools.riscv.tests.test_megrez_sdhci_gate -v
```

After a real Asterinas board run has completed the bounded read, publish the
complete log and atomic JSON result with the U-Boot CRC32 bound explicitly:

```bash
python3 tools/riscv/megrez_sdhci_gate.py \
  --transcript /absolute/path/to/megrez.serial.log \
  --output-dir /absolute/path/to/evidence \
  --expected-crc32 5f85f90e
```

The 2026-08-29 physical gate completed the exact 32 MiB read in 5.195899
seconds with CRC32 `5f85f90e`, then returned to U-Boot through the pre-boot
hardware watchdog. Both the board lifecycle result and this independent SDHCI
classifier reported pass.

## Megrez firmware framebuffer handoff

`megrez_board_session.py` can add the physically established 1920x1080
scanout at `0xfd800000` to the live DTB before `booti`. The change is RAM-only:
the tool never runs `saveenv`, and the default serial-only path remains
unchanged unless `--firmware-framebuffer` is present.

Asterinas currently selects only the first `console=` value for `/dev/console`.
The framebuffer gate therefore requires `console=tty0` to be first. The closed
`firmware-framebuffer` final profile returns success when the serial log has
observed the kernel register the handoff; Debian/systemd output after that is
expected on HDMI rather than on the serial console.

```bash
PYTHONPATH=tools/riscv python3 tools/riscv/megrez_board_session.py /dev/ttyUSB0 \
  --booti ASTERINAS_IMAGE_ON_BOOT_FS \
  --initrd STAGE1_INITRAMFS_ON_BOOT_FS \
  --dtb DTB_ON_BOOT_FS \
  --expected-crc32 booti=8hex,dtb=8hex,initrd=8hex \
  --bootargs "console=tty0 loglevel=info init=/init asterinas.reboot_after=600 -- --root-init=systemd" \
  --firmware-framebuffer \
  --final-profile firmware-framebuffer \
  --yes \
  --log /absolute/path/to/megrez-framebuffer.serial.log
```

This proves the current-main firmware framebuffer registration boundary. It
does not by itself prove Xorg, a desktop session, or native EIC7700 DRM.

## Opt-in Asterinas root serial console

The `debug-root-console` profile verifies a one-boot-only root shell created by
Stage1 under the `/run` tmpfs. It does not change the rootfs shadow database,
install a password, or persist a sudo rule. The exact selectors are accepted
only together:

```text
-- --root-init=systemd --debug-console=root
```

The physical graphics orchestrator instead uses
`--debug-console=isolated-root`. Stage1 then creates a transient
`/run/systemd/system.control/default.target` that points at the debug-console
target. The `system.control` lookup tier takes precedence over the rootfs's
`/etc/systemd/system/default.target`, so no rootfs service can start before the
orchestrator installs transient `/run/systemd/system.control` masks for the
competing network evidence services and starts the graphical target. The
same high-priority tier masks the generated serial getty so it cannot take the
UART away from the debug shell. The kernel command line deliberately does not
declare `console=ttyS0`; Stage1 gives the debug service explicit ownership with
`TTYPath=/dev/ttyS0`, without inviting systemd's console generator to create a
second owner. The physical boot combines `loglevel=off` with
`asterinas.klog_capture=info`: kernel records remain available to later
`dmesg` probes, while repeated writeback failures cannot saturate the
115200-baud control channel. This does not depend on systemd's
optional kernel-command-line generators, and the ordinary
`--debug-console=root` mode continues to boot the rootfs's normal default
target.

First run the bounded acceptance boot. Resolve the stable FTDI path rather
than relying on the current `ttyUSB0` number, retain the 120-second Asterinas
recovery timer, and require a fresh U-Boot epoch:

```bash
SERIAL=/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0
PYTHONPATH=. python3 tools/riscv/megrez_board_session.py "$SERIAL" \
  --booti ASTERINAS_IMAGE_ON_BOOT_FS \
  --initrd STAGE1_INITRAMFS_ON_BOOT_FS \
  --dtb DTB_ON_BOOT_FS \
  --expected-crc32 booti=8hex,dtb=8hex,initrd=8hex \
  --bootargs "console=tty0 console=ttyS0 loglevel=off init=/init asterinas.reboot_after=120 -- --root-init=systemd --debug-console=root" \
  --firmware-framebuffer \
  --final-profile debug-root-console \
  --milestone-timeout 150 \
  --require-recovery \
  --yes \
  --log /absolute/path/to/megrez-debug-root-bounded.serial.log
```

The gate sends only five fixed, read-only commands. It requires UID 0, PID 1
`systemd`, `/dev/mmcblk0p2 ext2`, and active desktop evidence/session units.
It also retains the complete framed exchange in the serial log on failure.

After that bounded gate passes, an operator handoff may omit only the recovery
timer and `--require-recovery`. The runner closes its descriptor after the
fixed probes, so the same root prompt can then be opened interactively:

```bash
PYTHONPATH=. python3 tools/riscv/megrez_board_session.py "$SERIAL" \
  --booti ASTERINAS_IMAGE_ON_BOOT_FS \
  --initrd STAGE1_INITRAMFS_ON_BOOT_FS \
  --dtb DTB_ON_BOOT_FS \
  --expected-crc32 booti=8hex,dtb=8hex,initrd=8hex \
  --bootargs "console=tty0 console=ttyS0 loglevel=off init=/init -- --root-init=systemd --debug-console=root" \
  --firmware-framebuffer \
  --final-profile debug-root-console \
  --milestone-timeout 120 \
  --yes \
  --log /absolute/path/to/megrez-debug-root-handoff.serial.log
picocom --baud 115200 "$SERIAL"
```

`ASTERINAS_DEBUG_CONSOLE_READY uid=0` identifies the Asterinas root console;
it requires no username or password. The `debian` / `debian` credentials below
belong only to the unrelated RockOS recovery system and never authenticate to
Asterinas.

This profile requires exactly one `loglevel=off`. On Megrez, asynchronous
kernel diagnostics and the shell share the physical UART; sustained block
errors can otherwise splice bytes into a framed shell response. Suppressing
kernel logging is limited to this fixed-command acceptance and operator
handoff profile. Use a separate diagnostic boot when kernel logs are the
evidence under investigation.

## Generic U-Boot `booti`

Build the deterministic marker initramfs, then provide it with a RISC-V Linux Image.

```bash
python3 tools/riscv/make_qemu_uboot_initramfs.py \
  target/qemu-uboot/marker-initramfs.cpio.gz
make test_riscv_uboot_booti \
  ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image" \
  ASTERINAS_INITRAMFS="$PWD/target/qemu-uboot/marker-initramfs.cpio.gz"
```

Generated U-Boot, DTB, disk, logs, and JSON evidence stay below `target/`.

## SiFive U

Use the same Asterinas artifacts to validate the SiFive UART path through U-Boot and userspace.

**The kernel must be built in Sv39 mode** (`FEATURES=riscv_sv39_mode`): the
QEMU `sifive_u` machine models only Sv39-capable harts (see the contract's
`mmu_types`), and a default Sv48 image page-faults on the early DTB read.
This is the single most common cause of a failed sifive_u run.

```bash
make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode
python3 tools/riscv/make_qemu_uboot_initramfs.py target/qemu-uboot/marker-initramfs.cpio.gz
make test_riscv_sifive_u \
  ASTERINAS_RISCV_BOOTI="$PWD/target/osdk/aster-kernel-osdk-bin.Image" \
  ASTERINAS_INITRAMFS="$PWD/target/qemu-uboot/marker-initramfs.cpio.gz"
```

An optional Linux control run uses the same machine and evidence path.

```bash
make test_riscv_sifive_u_linux_reference \
  RISCV_LINUX_IMAGE=/absolute/path/to/Image \
  RISCV_LINUX_INITRAMFS=/absolute/path/to/initramfs
```

The Asterinas run is accepted only after its userspace marker appears.
Firmware output alone is not sufficient.

## QEMU framebuffer display boot

Boot the kernel through U-Boot with a bochs display, inject a
`simple-framebuffer` DTB node, and watch the VT console render on the QEMU
display. This validates the firmware-framebuffer handoff software chain
(bochs -> U-Boot -> simple-framebuffer -> Asterinas VT) without hardware.
See `docs/porting/riscv-qemu-desktop.md` for the full setup and pitfalls.

The kernel must be Sv39 (`FEATURES=riscv_sv39_mode`) and the initramfs must
be the real marker initramfs (not the nix-build stub). Then run the driver:

```bash
make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode
python3 tools/riscv/make_qemu_uboot_initramfs.py target/qemu-uboot/initramfs.cpio.gz
# ... prepare_qemu_uboot_booti.sh prepare (see the doc for env vars)
python3 tools/riscv/qemu_desktop_boot.py             # headless + screendump
python3 tools/riscv/qemu_desktop_boot.py --display-gtk  # open a window
```

## LVGL desktop GUI (framebuffer interactive demo)

Build a static riscv64 `/init` that renders an interactive keyboard-navigable
desktop through LVGL on `/dev/fb0` (Home screen with three app cards, arrow
keys move focus, Enter opens, ESC returns), then packs it into the marker
initramfs. Verified on the QEMU virt display chain (bochs ->
simple-framebuffer -> VT -> userspace fbdev).

```bash
python3 tools/riscv/lvgl/build_lvgl_initramfs.sh   # -> target/qemu-uboot/initramfs-lvgl.cpio.gz
```

Then rebuild the boot disk with that initramfs and run the display boot
(see the "QEMU framebuffer display boot" section). The build clones LVGL
`v8.3.9` + lv_drivers `v8.3.0` into `target/lvgl` and applies the needed
patches (32-bit colors, resolution caps, fonts, enabled fbdev/evdev,
non-fatal FBIOBLANK). Compile in one gcc invocation to avoid
stale-object-color-depth mismatches. See `docs/porting/riscv-qemu-desktop.md`
for the interactive-GUI input verification status.

## Dependencies

Use the repository development container.
Preparing U-Boot additionally needs the RISC-V cross compiler, `dtc`, OpenSSL/GnuTLS development packages, and the Python development headers and `setuptools` used to build `pylibfdt`.
The unit tests use only the Python standard library and repository files.

### Host-side Megrez debugging

#### RockOS recovery login

The public factory-default credentials for the Milk-V Megrez RockOS/Debian
maintenance system are:

```text
username: debian
password: debian
```

A serial session on 2026-07-16 confirmed that this pair reaches
`debian@rockos-eswin`. It applies only to the board's RockOS recovery system,
not to the Debian root filesystem running on Asterinas. In particular, it
cannot unlock an Asterinas account whose shadow entry is `!` or `*`.
If an operator replaces the factory password, do not record the replacement in
the repository, shell history, or controller logs.

Keep builds and QEMU runs in the pinned development container.
On the host, install only the tools that observe the physical serial and Ethernet paths:

```bash
sudo apt-get install -y ethtool iperf3 arping
```

`ethtool` reports the host-side link state, negotiated speed, and error counters.
`arping` separates layer-2/ARP reachability from DNS, TCP, and the browser.
`iperf3` provides a controlled throughput test when a matching guest endpoint has explicitly been installed;
it is not a substitute for the HTTPS browser gate.

Packet decoding and screenshot OCR are optional:

```bash
sudo apt-get install -y tshark tesseract-ocr tesseract-ocr-chi-sim
```

The repository workflow already uses `picocom`/Python for serial I/O,
`tcpdump` for packet capture,
`dtc`/`fdtget` for device trees,
`gdb-multiarch` for debugging,
and e2fsprogs for Debian root images.
Do not install a second host QEMU merely for this workflow:
use the pinned container so that the emulator and cross-toolchain versions remain reproducible.

Verify the host once before a physical session:

```bash
for tool in docker picocom socat tcpdump ethtool iperf3 arping; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done
id -nG | tr ' ' '\n' | grep -qx dialout
test -r /dev/ttyUSB0 && test -w /dev/ttyUSB0
MEGREZ_HOST_IFACE=${MEGREZ_HOST_IFACE:-enp12s0}
ip -br link show "$MEGREZ_HOST_IFACE"
ethtool "$MEGREZ_HOST_IFACE"
```

The `dialout` check avoids running the serial gate as root.
`ttyUSB0` is the currently validated path;
resolve the actual USB-serial node again after unplugging or re-enumerating the adapter.
Broad passwordless `sudo` access is not required.

Profiles are reviewed code objects; command-line CPU, memory, bootarg, and resource-gate overrides are intentionally restricted.
Add a new machine by defining its contract and tests instead of adding board-specific branches to the runner.
