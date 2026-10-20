# Firefox renderer policy unit gate (2026-10-18)

## Gate

`firefox-renderer-policy-unit`

This is a host-side policy/unit gate only. It does not claim a new QEMU,
Firefox runtime, or physical-board result.

## Change

The approved gate policy accepts Firefox ESR's sanitized `Generic Renderer`
when `Compositing: WebRender` and `WebRender: available` are present. It still
rejects `llvmpipe`, `SWGL`, `softpipe`, and `software rasterizer` markers, and
rejects a missing renderer field. Both the about:support parser and the
browser-log parser preserve the observed renderer label. The DRM gate now
reports that observed renderer in `DEBIAN_FIREFOX_DRM_READY` instead of
hard-coding `virgl`. The fixed-scene gate applies the same policy to its
WebGL and MOZ-log paths, so a sanitized label is not rejected before the
independent `/dev/dri/renderD128` and screenshot checks run. The scene parser
now requires one `FIREFOX_DRM_ACCESS device=renderD128 read=1 write=1` record
before accepting a scene, so a renderer label cannot pass without an explicit
render-node access receipt.

## Red/green evidence

The new regression test initially failed against the old implementation:

```
FAIL: test_accepts_generic_renderer_after_firefox_sanitization
AssertionError: False is not true : GPU renderer does not identify virgl
```

After the minimal implementation change:

```
PYTHONPATH=. python3 -m unittest -q \
  tools.riscv.tests.test_firefox_webrender_gate \
  tools.riscv.tests.test_firefox_drm_scene_gate \
  tools.riscv.tests.test_firefox_runtime_provenance_gate
----------------------------------------------------------------------
Ran 30 tests in 0.006s
OK
```

Additional checks:

```
python3 -m py_compile tools/riscv/drm/firefox_webrender_gate.py \
  tools/riscv/debian/rootfs/firefox_drm_gate.py
# exit 0

Cross-gate regression set:

```
PYTHONPATH=. python3 -m unittest -q \
  tools.riscv.tests.test_firefox_webrender_gate \
  tools.riscv.tests.test_firefox_drm_scene_gate \
  tools.riscv.tests.test_firefox_runtime_provenance_gate \
  tools.riscv.tests.test_drm_render_node_gate \
  tools.riscv.tests.test_drm_gem_gate \
  tools.riscv.tests.test_drm_scanout_gate \
  tools.riscv.tests.test_drm_scanout_fault_gate \
  tools.riscv.tests.test_drm_virgl_fault_gate \
  tools.riscv.tests.test_drm_virgl_param_gate \
  tools.riscv.tests.test_drm_xorg_modesetting_gate
----------------------------------------------------------------------
Ran 100 tests in 2.508s
OK
```

`docs/porting/verify.py` still exits 1 only because the independent E3 and E5
runtime receipts remain blocked; its schema, manifest binding, and evidence
path diagnostics remain green.

## Final runtime diagnosis

The final official-package run passed the independent `renderD128`, EGL virgl,
and GLX virgl probes, but Firefox stopped at the bounded Marionette
`NewSession` phase. Its browser log identified the earlier environment defect
(`MOZ_AVOID_OPENGL_ALTOGETHER` was present despite value `0`); removing that
entry and extending the cold budget still produced no scene receipt. The
runtime boundary is recorded in
`2026-10-18-qemu-firefox-policy-r6-final.md`; no further automatic E3 rerun is
allowed for this input family.

git diff --check -- \
  tools/riscv/drm/firefox_webrender_gate.py \
  tools/riscv/debian/rootfs/firefox_drm_gate.py \
  tools/riscv/tests/test_firefox_webrender_gate.py
# exit 0
```

## Scope and limitation

This closes a policy inconsistency in the next E3 attempt; it does not reopen
the blocked E3 runtime receipt. The existing r44/r47 runtime failures remain
bound to the same log boundary and must not be rerun without a new external
input or runtime artifact.
