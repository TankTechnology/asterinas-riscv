# EGL pixel probe stage diagnostics (2026-10-18)

This bounded host-only check adds elapsed stage markers to
`tools/riscv/drm/egl-pixel-probe.py`. It does not start QEMU, Firefox, or a
physical-board boot. The markers make the next guest run distinguish a stall
in display discovery, EGL initialisation, context creation, framebuffer setup,
command completion, or pixel readback.

## Change

The probe now emits `PIXEL_PROBE_STAGE` with a monotonic elapsed time after:

`libraries` → `display` → `initialize` → `choose-config` → `context` →
`framebuffer` → `draw-finish` → `readback`.

The existing `PIXEL_PROBE_ERROR <stage> ...` output remains unchanged, so a
failure still names the first failed stage and the new markers do not alter the
gate decision.

## Bounded validation

Command (10-second host timeout, using `/dev/null` so no guest or board is
involved):

```text
timeout 10s python3 tools/riscv/drm/egl-pixel-probe.py /dev/null
```

Exit status: `0`.

Observed stage sequence:

```text
PIXEL_PROBE_STAGE name=libraries elapsed_ms=228.658
PIXEL_PROBE_STAGE name=display kind=gbm elapsed_ms=388.862
PIXEL_PROBE_STAGE name=initialize elapsed_ms=389.153
PIXEL_PROBE_STAGE name=choose-config elapsed_ms=389.213
PIXEL_PROBE_STAGE name=context elapsed_ms=401.313
PIXEL_PROBE_STAGE name=framebuffer elapsed_ms=401.362
PIXEL_PROBE_STAGE name=draw-finish elapsed_ms=448.152
PIXEL_PROBE_STAGE name=readback elapsed_ms=448.234
PIXEL_PROBE ok=yes left=ffffff right=000000 expect=ffffff/000000 renderer='llvmpipe (LLVM 15.0.7, 256 bits)'
```

The host result is a functional pixel-probe check only. Its renderer is
`llvmpipe`, so it is **not** evidence of virgl or hardware acceleration and is
not counted toward E3.

## Static checks

```text
python3 -m py_compile tools/riscv/drm/egl-pixel-probe.py   # passed
stage-order-check (the captured output)                        # passed
```

The raw bounded output is kept beside this document as
`2026-10-18-egl-pixel-stage-diagnostics-host.log`.

Artifact hashes:

```text
tools/riscv/drm/egl-pixel-probe.py
  9e6f22f35076206f4a3cfbf803a97cfa539ac0b819baf57d12ca7ee2d5b1d398
2026-10-18-egl-pixel-stage-diagnostics-host.log
  d7818c814f5337c504a9e65c864e1d78145e82853d6d65095f6e55d5cdf04db8
```

