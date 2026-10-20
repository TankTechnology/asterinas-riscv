# RISC-V Sv39 CPU handoff fix r55

## Root cause

The current kernel Image is built with `riscv_sv39_mode`. The Debian QEMU gate
was forcing `sv48=false,svadu=false,svade=true`; the same Image stopped before
serial initialization with that explicit CPU string. A 2-GiB TCG run using
QEMU's default A/D-update mode (`rv64,svpbmt=true,zkr=true`) entered
`riscv_boot` and completed OSTD and first-kthread markers.

The retained pre-yield Image is Sv48 and could boot under the old U-Boot path,
which had obscured the mismatch. The generic Debian gate is now aligned with
the default Svadu-compatible CPU mode. Dedicated Svade/Svadu conformance
profiles remain unchanged.

## Change

`tools/riscv/debian/rootfs/gate_protocol.py` now uses:

```text
rv64,svpbmt=true,zkr=true
```

The rootfs contract test was updated accordingly. No Firefox process was
started.

## Verification

- `python3 -m unittest tools.riscv.tests.test_debian_rootfs -q`: 225 tests pass.
- Direct QEMU, 2 GiB, SMP=4, current Sv39 Image, default CPU: reached
  `Enter riscv_boot`, `OSTD initialized`, and all first-kthread phase markers
  within 45 seconds.
- The same Image with explicit `sv48=false` stopped after OpenSBI; this is the
  negative control.
- The final U-Boot desktop gate attempt was terminated by the GL runtime
  container (exit 137/OOM) before a final receipt; no Firefox was launched.

Direct-run artifacts:

- `/mnt/mydisk/qemu-sv39-cpu-contract-r55/serial.log`
- `/mnt/mydisk/qemu-sv39-cpu-contract-r55/SHA256SUMS`
- `/mnt/mydisk/qemu-sv39-cpu-contract-r55/exit.txt`
