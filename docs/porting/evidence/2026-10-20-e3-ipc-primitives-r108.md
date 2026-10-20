# Asterinas IPC primitives probe r108

- gate: `e3-ipc-primitives`
- claim: E3 supplemental kernel/user IPC diagnostic
- status: `pass` for the generic IPC contract; this is not a Firefox scene pass

## Scope

The retained `tools/riscv/diagnostics/ipc_event_probe.c` was compiled as a
static RISC-V init and run once on the current Asterinas kernel with
`-m 512M -smp 2`, no Firefox, Xorg, DRM, network, or real hardware. The probe
covers the IPC operations used by Firefox's multiprocess startup path:
Unix socketpair, nonblocking receive, `SCM_RIGHTS` send/receive, epoll
readiness, descriptor read, child wait, and the second wait's `ECHILD` result.

## Guest result

All eight ordered phases emitted `ok=true`:

```text
empty_receive child_send epoll_ready rights_receive descriptor_read child_wait already_reaped complete
```

The init process then exited with status 0, so Asterinas printed its expected
“init process terminates” panic after the probe had completed. That post-probe
shutdown message is not a test failure.

This closes the generic Unix IPC/epoll/descriptor-transfer hypothesis for the
current kernel. It does not prove Firefox's higher-level Content/Marionette
state machine, which remains the E3 boundary.

## Artifacts

| artifact | path | SHA-256 |
| --- | --- | --- |
| probe source | `tools/riscv/diagnostics/ipc_event_probe.c` | repository source |
| RISC-V probe | `/tmp/ipcprobe-r107/root/init` (container) | `08cf0e7fee29b88442147a87100ec3a3ca51f965ed651e81cce77e3c9a224338` |
| probe initramfs | `/tmp/ipcprobe-r107/initramfs.cpio.gz` (container) | `0a645aa4161a97ebc2c2d9cc42d8f4424afe4ecc7a7a9f4e059ac30f12059a47` |
| boot disk | `/mnt/mydisk/ipc-event-r107.boot.ext4` | `4ad298dbee8550b8a3f18aa6f0493a54021f572f3425c125ff9589590f0acd9a` |
| serial log | `/mnt/mydisk/ipc-event-r107/serial.log` | `aad87d6ecc8ebffcdca89a6ef6855fd7db7b9326f9eac2635a4e1eaf7966e39e` |
| parsed receipt | `/mnt/mydisk/ipc-event-r107/parsed-result.json` | `64c68cb448c01695ba928dd454cd0f5724ea8ce87213ea31dd55931e1cb5b0ae` |

The first host runner recorded `status=timeout` only because it waited for an
overly exact terminal marker after the guest had already emitted the complete
phase. The parsed serial receipt above is the authoritative guest assertion.
