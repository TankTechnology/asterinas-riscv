# Debian RISC-V input gate

This gate proves the kernel-facing input path needed before booting a Debian
RISC-V userspace:

```text
QEMU VirtIO keyboard -> Asterinas input subsystem -> evdev -> guest probe
```

The guest dynamically finds the keyboard-capable `/dev/input/event*` node,
then verifies press and release events for `a`, `Shift+B`, Backspace,
and `Ctrl+C`.
The runner uses four harts and records the serial transcript,
the exact QEMU command line,
and SHA-256 identities for every boot artifact.

This milestone does **not** build or boot a Debian root filesystem.
It does not cover Debian package installation, systemd, a graphical session,
or a browser.
Those stages should consume this gate as an input-subsystem prerequisite.

## Debian provenance

[Debian 13 "trixie" cloud images](https://cloud.debian.org/images/cloud/trixie/latest/)
publish official RISC-V 64-bit image artifacts.
The [Debian 13 release metadata](https://deb.debian.org/debian/dists/trixie/Release)
lists `riscv64` as a release architecture.
Use those two Debian-operated locations as the image and package provenance
when a Debian rootfs milestone is added.

## Unit tests

Run the host protocol, guest state-machine, archive,
orchestration, cleanup, and evidence tests before QEMU:

```bash
make test_riscv_debian_input_unit
```

## Build the initramfs

Run all commands from the repository root.
The cached RISC-V development image already provides QEMU,
the cross compiler, `cpio`, and DT tooling.
Install its missing cross-libc and Linux UAPI development packages in the
ephemeral container before invoking the deterministic builder:

```bash
install -d -m 0755 target/debian-riscv/input-gate
docker run --rm --privileged --network=host \
  -v /dev:/dev \
  -v "$PWD:/root/asterinas" \
  -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  bash -lc '
    apt-get update &&
    apt-get install -y --no-install-recommends \
      libc6-dev-riscv64-cross linux-libc-dev-riscv64-cross &&
    bash tools/riscv/debian/build_input_gate.sh
  '
```

The output is the raw, reproducible newc archive
`target/debian-riscv/input-gate/initramfs.cpio`.
Its only entries are `.` and the static RISC-V `/init` probe.

## Build the current-main Sv39 kernel

Start from an up-to-date `main` worktree and build the kernel in Sv39 mode.
The gate's CPU contract explicitly disables Sv48,
so a default Sv48 kernel is not a valid test artifact.

```bash
docker run --rm --privileged --network=host \
  -v /dev:/dev \
  -v "$PWD:/root/asterinas" \
  -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  bash -lc 'make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode'
```

The gate consumes
`target/osdk/aster-kernel-osdk-bin.Image`.

## Generate an SMP=4 DTB

Do not use `target/qemu-uboot/current/qemu-virt.dtb` for this gate.
That cached payload may describe only one CPU,
while this gate boots QEMU with four harts.
Generate a private DTB with the same machine, CPU, memory,
and hart count as the gate itself:

```bash
install -d -m 0755 target/debian-riscv/input-gate
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v "$PWD:/root/asterinas" \
  -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  qemu-system-riscv64 \
    -machine virt,dumpdtb=target/debian-riscv/input-gate/qemu-virt-smp4.dtb \
    -cpu rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true \
    -m 2G -smp 4 -display none
```

The runner rejects a DTB whose enabled CPU-node count differs from `--smp`.

## Run the QEMU gate

Reuse the reviewed generic-Sv39 U-Boot binary at
`target/qemu-uboot/cache/u-boot-build/u-boot`.
The runner builds its own private ext4 boot disk below the gate output directory;
it does not overwrite `target/qemu-uboot/current`.

```bash
install -d -m 0700 target/debian-riscv/input-gate/run
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v "$PWD:/root/asterinas" \
  -w /root/asterinas \
  asterinas/asterinas:0.18.0-20260702-riscv-cross-dtc-cached \
  python3 tools/riscv/debian/input_gate.py \
    --kernel target/osdk/aster-kernel-osdk-bin.Image \
    --uboot target/qemu-uboot/cache/u-boot-build/u-boot \
    --dtb target/debian-riscv/input-gate/qemu-virt-smp4.dtb \
    --initramfs target/debian-riscv/input-gate/initramfs.cpio \
    --output-dir target/debian-riscv/input-gate/run \
    --smp 4
```

The two required serial markers are:

```text
__DEBIAN_INPUT_GATE_READY__
__DEBIAN_INPUT_GATE_PASS__
```

Check the durable evidence after QEMU exits:

```bash
grep -F '__DEBIAN_INPUT_GATE_' \
  target/debian-riscv/input-gate/run/serial.log
python3 -m json.tool \
  target/debian-riscv/input-gate/run/result.json
```

A passing `result.json` has `passed: true`, `smp: 4`,
an empty `panics` list,
and SHA-256 values for U-Boot, the private boot disk,
the kernel, DTB, and initramfs.
Keep `serial.log` and `result.json` together;
the JSON result is published only after the complete transcript is stored.
