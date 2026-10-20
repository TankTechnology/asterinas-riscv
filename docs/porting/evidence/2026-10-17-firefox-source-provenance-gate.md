# Firefox source provenance gate

Date: 2026-10-17

## Scope

This closes the bounded host-side provenance check for the two Firefox ESR
compatibility patches. It verifies the pinned source-file hashes and patch
hashes, then applies both patches in order with GNU `patch --dry-run --fuzz=0`
to a disposable source tree. The supplied source tree is never modified. This
is a source-integrity result only; it is not a Firefox build, package-signing,
QEMU runtime, WebRender, or physical-board result.

## Inputs

- Manifest: `tools/riscv/debian/rootfs/firefox_compat/source_patch_manifest.json`
- Gate: `tools/riscv/debian/rootfs/firefox_compat/source_patch_gate.py`
- Patches: `0001-recognize-virgl-webgl-renderer.patch` and
  `0002-prefer-drm-render-node-for-webrender.patch`
- Pinned source: Debian `firefox-esr 140.16.0esr-1~deb13u1`
- Source fixture: `/tmp/firefox-source-gate-run.HFKX1y`, assembled from the
  read-only audited files in `/tmp/firefox-esr-audit.l06p` under their
  manifest paths.

The four source hashes verified by the gate are:

```text
594cc7306a5617f03a73b91f42db6c8f4276c8f71123602cb27da3cb0ee8c27e  dom/canvas/SanitizeRenderer.cpp
1d187a13387f9e4f3d838e9480135459ceb5cd194ba53b78e88ec4b88efd6293  dom/canvas/gtest/TestSanitizeRenderer.cpp
39e2cbb4854daff33903126a78900aca61e8312b3c991bd6b68427f5cca721a2  gfx/gl/GLLibraryEGL.cpp
7cab444255b31f7c4aee92f8297a19b6abe3bbf97b9f6d56d90e0ce15611e213  gfx/gl/GLContextProviderEGL.cpp
```

## Verification

The independent unit and CLI checks were run from the repository root:

```text
$ PYTHONPATH=tools/riscv python3 -m unittest -q \
    tools.riscv.tests.test_firefox_source_patch_gate
----------------------------------------------------------------------
Ran 5 tests in 0.073s

OK

$ PYTHONPATH=tools/riscv python3 tools/riscv/debian/rootfs/firefox_compat/source_patch_gate.py \
    --source-root /tmp/firefox-source-gate-run.HFKX1y
FIREFOX_SOURCE_PATCH_GATE PASS patches=2 source_files=4 fuzz=0

$ PYTHONPATH=tools/riscv python3 -m unittest -q \
    tools.riscv.tests.test_firefox_drm_scene_gate \
    tools.riscv.tests.test_firefox_webrender_gate
----------------------------------------------------------------------
Ran 21 tests in 0.003s

OK
```

The gate's temporary staging directory was discarded after the dry run; no
patch was applied to the audited source fixture. Python bytecode compilation
of the gate and its tests also passed.

The same command was then run against the actual extracted Debian source tree
`/tmp/firefox-esr-140-source-tree/firefox-140.16.0` after the core orig and
Debian tarballs were downloaded and checked against the source-package
manifest. The real tree produced the same four hashes and:

```text
FIREFOX_SOURCE_PATCH_GATE PASS patches=2 source_files=4 fuzz=0
```

This is the first source gate result against the full source layout rather than
the small read-only fixture.

## Result and boundary

**PASS:** two patches, four source hashes, ordered zero-fuzz applicability,
and the existing Firefox host-gate regression suite are reproducibly checked.
The next gate is an authenticated, provenance-recorded Firefox ESR source or
package build containing both patches, followed by the strict QEMU WebRender
scene gate. Until those exist, Task 6 remains incomplete and no hardware
acceleration claim is made.
