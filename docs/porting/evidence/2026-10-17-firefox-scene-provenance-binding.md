# Firefox scene provenance binding

Date: 2026-10-17

## Change

The host scene parser now has an explicit provenance-bound entry point:

```text
tools/riscv/drm/firefox_drm_scene_gate.py::classify_scene_with_runtime
```

When the optional `--packages-lock` and `--source-manifest` arguments are
provided, a scene is accepted only after both conditions pass:

1. the fixed transcript proves WebRender, virgl, screenshot geometry, frame
   samples, and restart recovery;
2. the selected rootfs package lock identifies the exact Firefox package and
   version covered by the source patch manifest.

The result carries the package version and `packages_lock_sha256`, so a future
QEMU receipt can be tied to the rootfs identity used for that run. Calling the
parser without these arguments retains the old transcript-only behavior for
legacy diagnostics; promotion runs must pass both arguments.

## Host gate verification

The focused scene and runtime identity suites pass:

```text
$ PYTHONPATH=tools/riscv python3 -m unittest -q \
    tools.riscv.tests.test_firefox_drm_scene_gate \
    tools.riscv.tests.test_firefox_runtime_provenance_gate
Ran 20 tests ...
OK
```

A deterministic complete transcript with the matching lock produces:

```text
passed=true package=firefox-esr package_version=140.16.0esr-1~deb13u1 \
packages_lock_sha256=f3f63d893702f7a1a97d2cd7fdcf063de475069f437cf8af25cefa875b4e910a
```

The same transcript paired with the current `browser-web-drm` lock is
rejected before promotion:

```text
passed=false reason="runtime package identity: version mismatch: 153.4.0esr-1~deb13u1; expected 140.16.0esr-1~deb13u1"
```

These two transcript runs are parser contract tests, not QEMU runtime
evidence. The existing QEMU r46 receipt remains a real negative runtime result
(`llvmpipe`); no Firefox WebRender pass has been claimed.

## Next gate

Build and provenance-admit a real ESR RISC-V package containing both source
patches, create a rootfs with its matching package lock, and run the strict
QEMU scene through this provenance-bound entry point. The current host still
lacks the pinned builder image and has insufficient free space for that build.
