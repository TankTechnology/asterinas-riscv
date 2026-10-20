# futex-lost-wakeup r105

- gate: `futex-lost-wakeup`
- claim: E3 supplemental kernel/user IPC diagnostic
- status: `pass` for the bounded reproducer; this does **not** claim the Firefox E3 scene passed
- manifest SHA-256: `68645b154bb876db055a7e49b32a55b0f26e3a4bab44e89be4b2223f7abd39f8`

## Scope

This was one bounded run of the existing static probe as the only Asterinas init. It used `-m 512M -smp 2`, no Firefox, Xorg, DRM, network, serial write, or real hardware. The host control used the same C source and default 100,000/100/1,000 rounds.

## Results

RISC-V Asterinas QEMU completed in 42.635 seconds:

```text
FUTEX_H1 ping-pong rounds=5000 completed=5000 timeouts=0 errors=0 waits=2955 wakes=2850 signals=3817 status=pass
FUTEX_H2 detail ready=4 moved=1 woken=3 failed=0
FUTEX_H2 requeue rounds=1 waiters=4 failures=0 status=pass
FUTEX_H3 ppoll-pipe rounds=1 timeouts=0 errors=0 status=pass
FUTEX_LOST_WAKEUP_PROBE status=pass
```

The same source passed the x86_64 control (`rc=0`):

```text
FUTEX_H1 ping-pong rounds=100000 completed=100000 timeouts=0 errors=0 waits=7068 wakes=213 signals=203 status=pass
FUTEX_H2 detail ready=4 moved=4 woken=4 failed=0
FUTEX_H2 requeue rounds=100 waiters=4 failures=0 status=pass
FUTEX_H3 ppoll-pipe rounds=1000 timeouts=0 errors=0 status=pass
FUTEX_LOST_WAKEUP_PROBE status=pass
```

## Interpretation

H1 (signal-interrupted futex ping-pong), H2 (FUTEX_REQUEUE), and H3 (`ppoll` plus pipe) are not reproduced on the current bounded RISC-V image. The serial diagnostic also recorded both long waits ending with `outcome=woken`. No speculative `pause_timeout` or futex semantic patch is justified by this run. The Firefox NewSession boundary therefore remains Firefox-specific guest IPC/scheduling, rather than a demonstrated generic futex lost wakeup.

## Artifacts

| artifact | path | SHA-256 |
|---|---|---|
| RISC-V boot disk | `/mnt/mydisk/futex-lost-wakeup-r8/boot.ext4` | `738b6b733aa188482bf0a3e57f539947b7357e43c0e8bdcddaabfa1f6b8571a5` |
| kernel in boot disk | `/mnt/mydisk/futex-lost-wakeup-r8/boot.ext4:/asterinas.booti` | `829f9fa348d15bc3826c5543aea0ade0408c241d816e4d48db53de4007249297` |
| initramfs in boot disk | `/mnt/mydisk/futex-lost-wakeup-r8/boot.ext4:/initramfs.cpio.gz` | `18dcf269869b647571c33194a0d0f7eb1d38df8733a60dca825f2edc7f233268` |
| RISC-V probe | `/mnt/mydisk/futex-lost-wakeup-r8-probe-riscv64` | `ea7ee52cdf662fa57852b790cdfe5bf39a87bf8f6501e70e2458670fb50f2e35` |
| RISC-V serial | `/mnt/mydisk/futex-lost-wakeup-r105/serial.log` | `5f2b8a13aa3682007cea493d55b00e4ad3d7d1bceb975ae308475485e4932a05` |
| runner result | `/mnt/mydisk/futex-lost-wakeup-r105/result.json` | `aeed1b76b34cc14c8bc40351ee10ad47ba8ae320618e557d4fa83db0c53b329a` |
| x86_64 probe | `/mnt/mydisk/futex-lost-wakeup-r105/futex-lost-wakeup-x86_64` | `71439f43f5ae04e275b0d6b67e668b0e597a145bb32085ed607bcd126e3578f2` |
| x86_64 output | `/mnt/mydisk/futex-lost-wakeup-r105/x86.log` | `72aab1af76e3913fddae9ff5fd36f2497ede230f4780ac33349fa12131124916` |
