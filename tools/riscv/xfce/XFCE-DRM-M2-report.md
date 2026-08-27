# RISC-V Xfce DRI3 client acceleration

Date: 2026-08-27

## Result

The RISC-V Xfce path now uses virtio-gpu/virgl for both sides of the desktop stack:

- Xorg reports `glamor X acceleration enabled on virgl`;
- Mesa reports `Using DRI3 for screen 0`;
- the GLX acceptance program reports `XFCE_GL_DIRECT yes` and an
  `XFCE_GL_RENDERER virgl` renderer;
- the shader, pixel-readback, and frame-timing gate reports
  `XFCE_GL_BENCH_PASS`;
- the complete systemd/Xorg/Xfce gate reports `XFCE_DRM_PASS`.

This is the first Xfce gate that proves application-side direct rendering.
Earlier Xorg runs used virgl for glamor, but GLX applications still selected
`llvmpipe`.

## Kernel defects found

### Lost control-queue progress

The first IRQ-only control-completion change (`287ad2171`) could leave a
synchronous GPU request asleep after a lost, suppressed, or coalesced wakeup.
The parent (`7dd1c3d2a`) reached virgl glamor, while `287ad2171` repeatedly
stalled during glamor initialization.

Commit `771da1018` restores guaranteed progress: synchronous waits actively
drain the used ring as a fallback while retaining IRQ-driven completion for
normal operation and asynchronous fences. The current kernel then completed
the full Xfce virgl boot again.

### Missing DRM device identity for DRI3

libdrm's `drmGetDeviceNameFromFd2` reads
`/sys/dev/char/226:0/uevent` and extracts `DEVNAME` before Xorg can reopen the
DRM node for a DRI3 client. Asterinas exposed PCI identity below
`/sys/dev/char/226:0/device/uevent`, but did not expose the top-level DRM
minor `uevent`. Consequently Xorg initialized the DRI3 extension but every
client received `screen 0 does not appear to be DRI3 capable` before any DRM
authentication ioctl was issued.

The sysfs tree now reports `DEVNAME=dri/card0` and
`DEVNAME=dri/renderD128` for the two DRM minors. Xorg can therefore reopen the
primary node and pass its file descriptor to the client.

### Missing legacy primary-node authentication

The DRI3 open path obtains a per-file token with `DRM_IOCTL_GET_MAGIC` and asks
the current DRM master to consume it through `DRM_IOCTL_AUTH_MAGIC`.
Asterinas now allocates stable nonzero tokens, invalidates them after
authentication or close, marks the target file authenticated, and permits
authentication-gated `GEM_FLINK` and `GEM_OPEN` operations.

DRM master set/drop operations also follow the Linux permission model: the
same process may reuse a file that was previously master, while another
process requires `CAP_SYS_ADMIN` in the initial user namespace.

## Renderer benchmark

`gl_renderer_bench.c` creates a direct GLX context, compiles a small fragment
shader, renders three warm-up frames plus 30 measured frames, checks one output
pixel, and reports the renderer and elapsed time to the serial console.
The boot harness rejects a virgl run unless all of these observable gates are
present.

One TCG sample on the same 320x240, 30-frame workload measured:

| Client renderer | Elapsed | FPS |
|---|---:|---:|
| `llvmpipe` before DRI3 | 49.284 s | 0.609 |
| `virgl` through DRI3 | 5.588 s | 5.369 |

The measured sample is about 8.8 times faster. Absolute TCG results remain
host-load-sensitive, but the renderer identity and direct-context gate are
deterministic and establish that application commands are reaching virgl.

The final no-probe regression run completed the same workload in 1.499 seconds
(20.011 FPS) and reached the complete desktop gate in 2 minutes 23.311 seconds.
That spread is why the report uses the more conservative first comparison and
does not treat an individual TCG FPS result as a stable performance number.
Its serial log is retained outside Git at
`target/xfce-drm/serial-dri3-final.log` with SHA-256
`51bce1323ac93df724dd1f9cd22d0cd2a122b3a6d1252c5638ccc479301b8e5e`.

The virglrenderer console still reports one rejected
`CREATE_OBJECT`/command-buffer sequence during desktop startup. The benchmark
continues to produce the expected pixel and completes successfully, so this is
tracked as a remaining command-stream compatibility issue rather than evidence
that all 3D commands are complete.

## Reproduce

Build the persistent root after the Debian RISC-V runtime has been fetched:

```sh
python3 tools/riscv/xfce/build_xfce_drm.py
```

Build the current RISC-V kernel and run the noninteractive acceptance gate:

```sh
VDSO_LIBRARY_DIR=/path/to/linux-vdso make kernel TARGET_ARCH=riscv64
python3 tools/riscv/xfce/boot_xfce_drm.py --timeout 700 --settle 3
```

Use `--interactive` to keep the QEMU GTK window open after the same gate has
passed. `--software-display` remains available as the llvmpipe baseline, and
`--fbdev-display` remains the no-DRM fallback.

## Remaining work

- Identify and implement the virgl command rejected during desktop startup.
- Add repeatable interaction/frame-latency measurements; boot time is dominated
  by RISC-V TCG user-space execution.
- Switch the official Debian desktop profile from its current bochs/fbdev gate
  to this DRM path while retaining fbdev as a fallback.
- Restore the RISC-V ktest build; it currently stops in the unrelated
  `riscv_flush_icache` syscall because this branch lacks
  `ostd::arch::flush_icache`.
