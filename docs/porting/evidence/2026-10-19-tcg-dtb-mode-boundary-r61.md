# TCG DTB mode boundary comparison r61

This is a bounded direct-kernel comparison. It did not start Stage1, Xorg, Firefox, networking, or real hardware. Each run used the same current Sv39 Image, `-m 2G`, `-smp 4`, and `rv64,svpbmt=true,zkr=true`; only the DTB source differed.

| Run | DTB | timeout | ordered markers | serial SHA-256 |
|---|---|---:|---|---|
| TCG generated | QEMU default `sv57` DTB | 60 s, exit 124 | `Enter riscv_boot` → `OSTD initialized` → `first-kthread-enter` | `fbf906e9a0a978d390493136fb810dbc022cb992df823f0a71cbda73ca9fa6fe` |
| Explicit Sv39 | `/mnt/mydisk/asterinas-gates/qemu-virt-4cpu-sv39-20261017.dtb` (`1bcec907d28a9ede2c0020d9024de93640d2d0ba9bdfc5f09f20e81f4777a9eb`) | 60 s, exit 124 | `Enter riscv_boot` → `OSTD initialized` → `first-kthread-enter` | `6aa976c126e06f5361c58026da7f54d22422db87452ec98b79b4b7d679a5cf0a` |

Both paths reached the same early boundary. The explicit Sv39 run produced more serial output but did not reach Stage1 within the bound. This rules out a DTB-mode difference as the cause of the *pre-first-kthread* TCG failure; it does not yet prove equivalence of the later rootfs handoff. The next useful comparison is a short U-Boot/Stage1 run with the same two DTB artifacts, not another Firefox run.

Artifacts:

- `/mnt/mydisk/qemu-dtb-mode-compare-r61/tcg_sv57/`
- `/mnt/mydisk/qemu-dtb-mode-compare-r61/explicit_sv39/`
