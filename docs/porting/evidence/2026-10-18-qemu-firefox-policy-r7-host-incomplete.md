# E3 official Firefox policy-r7 host publication boundary (2026-10-18T13:16:40Z)

This is the single bounded run started with the r7 overlay after the
Xauthority runtime-path correction. It is recorded as a host-harness
incomplete run; it is not a guest rendering result.

## Command boundary

The run used the GL-capable QEMU container, `--display egl-headless`,
`--device /dev/dri`, the retained custom QEMU binary, `--nic none`, and
`--boot-timeout 900 --command-timeout 60 --cleanup-timeout 20`. Inputs were
`target/e3-render-policy-r1/rootfs/debian-root.ext2`, the current kernel,
DTB, Stage1, U-Boot, package manifest and scene source. No physical board
was accessed.

## Durable observation

- Start boundary: `2026-10-18T13:06Z` (host local 21:06), overlay `overlay-r7.json`.
- The gate created `target/e3-render-policy-r1/qemu-r1/` at approximately
  `2026-10-18 21:12:52 +0800`, mode `0700`, owner `uid=0,gid=0`.
- At the host inspection boundary `2026-10-18T13:15:20Z`, the directory was
  empty: no `result.json`, no serial transcript, and no stage transition
  record were durable.
- The QEMU and gate processes had exited by that inspection. Because the
  publication artifacts are absent, no guest serial boundary can be
  authenticated from this run.

This closes the evidence-loss classification only: the official Firefox
scene remains unproven. It must not be interpreted as a Firefox, virgl, or
Asterinas guest failure, and it does not replace the prior r6 NewSession
`timed_out` receipt.

## Inputs

- manifest SHA-256: `68645b154bb876db055a7e49b32a55b0f26e3a4bab44e89be4b2223f7abd39f8`
- kernel SHA-256: `cabc01961888055d64b5f0d59a1a15a1d797e95e969fca7de7d61dfe2429328d`
- root image SHA-256: `aa26ef744b3c0e8568bbbaafbbfe300199ef14e354a698410b8b73bab4b80ad7`
- DTB SHA-256: `1bcec907d28a9ede2c0020d9024de93640d2d0ba9bdfc5f09f20e81f4777a9eb`
- Stage1 SHA-256: `ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84`
- scene HTML SHA-256: `fde6bed9e11dcaa4e988dee369cc75490293d1236f6df8fdc93481dd159ad04c`

## Classification

`host_harness_incomplete`: the run produced no durable output. The next
necessary change is durable running/termination publication (fsync+rename)
and a persistent output directory before another E3 runtime attempt. No
automatic rerun is authorized by this receipt.
