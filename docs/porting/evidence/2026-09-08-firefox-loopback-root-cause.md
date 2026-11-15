# Firefox loopback stall: System V SHM root cause and repair

## Outcome

The first causal kernel defect was the lifetime of a System V shared-memory
segment after `shmctl(IPC_RMID)`. Asterinas removed the ID and freed it
immediately. Linux marks an attached segment for deletion, keeps the ID usable,
and destroys the segment only after its final detach.

Firefox's GTK shared-image path creates an MIT-SHM image in this order:

```text
Firefox: shmget -> shmat -> shmctl(IPC_RMID)
X server:                            shmat(the same shmid)
```

The former Asterinas implementation made the X-server `shmat` fail with
`EINVAL`. MIT-SHM was consequently unavailable and Firefox fell back to the
X11 image-transfer path. That increased X11 socket traffic and backpressure
enough that the Firefox Socket Thread stopped dispatching the fourth
Marionette request. The earlier observation—1176 bytes sent completely while
Firefox remained in a three-fd `ppoll`—was therefore a downstream symptom, not
a TCP or `ppoll` defect.

The relevant upstream and ABI references are:

- [Firefox ESR 140 `nsShmImage.cpp`](https://sources.debian.org/src/firefox-esr/140.15.0esr-1/widget/gtk/nsShmImage.cpp/)
- [Linux `shmat(2)` semantics](https://man7.org/linux/man-pages/man2/shmat.2.html)
- [Linux System V SHM implementation](https://github.com/torvalds/linux/blob/master/ipc/shm.c)

The QEMU result below is browser acceptance, but it is not physical-board
acceptance. Megrez USB, framebuffer scanout, and HDMI evidence remain a
separate final gate.

## Why the earlier hypothesis was rejected

The short experiments deliberately separated transport from rendering:

- the multi-Pollee kernel tests passed;
- the persistent loopback TCP/three-fd `ppoll` test passed with default Nagle
  and `TCP_NODELAY` on both the Linux reference and Asterinas;
- bounded TCP events reached send, dispatch, peer processing, readiness, and
  poll wake boundaries;
- Linux Firefox completed the exact four-command Marionette sequence;
- disabling MIT-SHM changed Linux's behavior toward the same high-volume X11
  fallback seen in Asterinas.

This rejected a simple lost TCP packet or missed poll wake. Source analysis
then exposed the cross-process `IPC_RMID` sequence, and the focused SHM probe
made that single semantic difference executable without Firefox.

## Causal regression and repair

`tools/riscv/diagnostics/sysv_shm_rmid_probe.c` uses raw RISC-V syscalls and no
libc. It creates and attaches a segment, marks it for removal, and attaches the
same shmid again. The second mapping verifies shared data; after both mappings
detach, a new attach must fail. It also makes an intentionally unaligned
`shmat` call and proves that the rejected mapping did not retain a phantom
attachment count.

The Linux reference accepts the second attach. The former Asterinas behavior
rejected it. The repaired Asterinas run prints:

```text
System V SHM deferred removal probe passed.
```

The final A/B uses one probe archive and identical QEMU arguments. The frozen
pre-rollback-fix kernel reports:

```text
System V SHM failed attach retained a phantom attachment.
```

The repaired kernel reports the pass line above. The retained logs are:

| Run | Kernel SHA-256 | Log SHA-256 |
| --- | --- | --- |
| Rejected-attach rollback RED | `23f5dafd97b9344b6580efe264a3e40d595414860a03d1905c1afd414781f81c` | `5b77a1cbc15b74e15ba1bfc694e5fbf255a5fd441786780fffcc5146036bf184` |
| Rejected-attach rollback GREEN | `c09c4210d242d9db74d379ff249b1b16cf8d1e3fb8663015fcdd2e549143d719` | `ff962f43614f8a9cda37ff1acf5a13677108495194f51b2d58ac49ccedfb8d4a` |

The probe executable SHA-256 is
`a35c6d04d261edd9aaf4b23a2040035151e36c2d47c634a91a733a1ffe269330`.
The final initramfs SHA-256 is
`01aaef0ee135d4440781d6c3cb70c5d29d935b18b9be02b7addd8c6a759a7d39`.

The permanent regression
`test/initramfs/src/regression/ipc/shm/sysv_shm.c` additionally checks
`SHM_DEST`, the attachment count, cross-process data, destruction after the
last detach, ordinary access permissions, and the `CAP_SYS_ADMIN` removal
case.

The kernel repair:

- atomically marks or removes an IPC object under the ID-table write lock;
- keeps an attached, removed SHM segment in the namespace;
- reserves the attachment before constructing the VM mapping and rolls it
  back if mapping fails;
- removes the address record only after `vmar.remove_mapping` succeeds;
- destroys and frees the ID on the final detach;
- reports `SHM_DEST` through `IPC_STAT` while deletion is pending;
- applies Linux-style owner/mode and IPC-namespace capability checks.

No `unsafe` code was added to `kernel/`.

## Observer-disabled browser acceptance

The final QEMU gate uses the ordinary browser root rather than the diagnostic
Firefox archive. It runs the same Firefox process for three distinct cycles;
each cycle enters a new 16-character nonce using the virtual keyboard, moves
the absolute tablet, clicks once, verifies trusted DOM events, captures a
guest PNG, and captures the QEMU framebuffer.

The successful plan-bound result is
`target/current-main-physical-graphics/physical/desktop-plan-bound-final/result.json`,
SHA-256
`b1b3cd87f71633fd0696f2204a38418a4e6377d4dc172f2cb257ea809771140d`.
Its native result SHA-256 is
`dee1db953372e687a5df954451772d9d7dc690683b35ead70167d99981989fd8`,
and its serial transcript SHA-256 is
`50e6cf5233fd6c46b23b0de65c3b429b54a284049d733d6fa03558240768f1c6`.
The result is bound to frozen plan SHA-256
`ccb815207c2eefa31f91d858c452dd8e7e69a306f069059a195acea2ce7069fd`.

| Property | Value |
| --- | --- |
| Result | `passed=true`, `reason=pass`, `profile=browser-web`, `physical=false` |
| Debian | `13.6` |
| Kernel image | `c09c4210d242d9db74d379ff249b1b16cf8d1e3fb8663015fcdd2e549143d719` |
| Root image | `beed8e4022b9fa0be238b9ef99ab73f5a72d32471d0db71ab3b5885108454d2f` |
| Root manifest | `a14e43d2ac528842f3b0535ebf220c99cf8b644816ff114ab1b3b40a24924b92` |
| Cycle 1 guest PNG | `b433fdec33b9d7f20fe132156ddbe0a2d576c0bd37868c5d7818b19c941b48da` |
| Cycle 2 guest PNG | `c95cf507799f69e70c70e0eb252c9cfbddd3dbe81747ee4329bffde7ab2288c3` |
| Cycle 3 guest PNG | `40606d6e3d14b79833980559b34ab87f452dbd1c68372a0e54f5e72956e62a6e` |

All three images were also inspected visually. They show the expected cycle,
nonce, `trustedKey`, `trustedInput`, `trustedPointer`, `trustedClick`, one click,
and the cyan pass state. The distinct guest and framebuffer hashes reject a
stale screenshot being reused across cycles.

## Plan-bound software-reboot acceptance

The same final kernel also passed the protected recovery gate. Before reboot,
the guest completed the ordered 16 KiB, 64 KiB, 1 MiB, and 16 MiB network
transfers, for 17,907,712 bytes total. The gate then observed the armed
60-second software-reboot marker, a second OpenSBI epoch, U-Boot 2026.07, and a
fresh U-Boot prompt. The 64 MiB boot disk hash was unchanged across the run.

The native recovery result is
`target/qemu-uboot/recovery-firefox-final/result.json`, SHA-256
`82efc273ab8e58776ff49f280f42698a8787a10fa119562989447982acdf0d13`.
The plan-bound recovery evidence SHA-256 is
`8aea448abf460c110457ce3eacb42702f7cb64743a1f5aeab241a2e096fbc0f4`;
its serial SHA-256 is
`ac648d2416b326eb99706f3fcdfea0580b6fa82b830178881ed540f300839bac`.

## Claim boundary and next gate

The repaired kernel has closed the Firefox software interaction loop in RISC-V
QEMU. The remaining acceptance work is operational and hardware-specific:

1. install the exact 2 GiB root image to the Megrez test partition;
2. boot the same kernel on Megrez;
3. complete three nonce-bound interactions using a real USB keyboard and
   relative mouse;
4. retain a newly created HDMI capture of the final cyan page and verify the
   automatic recovery boundary.

Until those four steps pass, this record does not claim physical graphics
acceptance.
