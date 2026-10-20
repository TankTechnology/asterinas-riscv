# E3 Firefox remote-trace probe r110

## Scope

One bounded probe was prepared with the opt-in `remote.log.level=Trace`
diagnostic switch. It used the same kernel, DTB, U-Boot and official Debian
Firefox rootfs as the preceding probe family. No full scene, real hardware,
serial write or reboot was performed.

## Host boundary

The QEMU command entered `lifecycle.json` with `phase=launch,status=running`,
but the persistent Docker container `asterinas-qemu-gl-env-r2` reached its
fixed one-hour lifetime and exited with code 0 before it could publish a
serial log or terminal `result.json`. `docker inspect` showed
`OOMKilled=false`; this is a host-container lifetime failure, not a guest
Firefox result. The lifecycle file remains `status=running` because the host
owner disappeared before its finalizer; it must not be used as a runtime
receipt.

## Prepared input identity

- root image: `77b0f3dff3095573bb4d44214e23825d1163d9dd3ea802167502c8d319c68b19`
- kernel: `38982e2e1b81b4bdd7649d9e9270022a705afd964ac77b66d5cacc26b1ae81be`
- DTB: `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c`
- output directory: `/mnt/mydisk/qemu-firefox-probe-trace-r110`

The trace switch and its profile preference were covered by the focused unit
suite (36 tests passed), but no guest trace marker exists. The next attempt
must start a fresh persistent container with a lifetime exceeding the full
host bound before launching QEMU; do not resume this stale lifecycle.

