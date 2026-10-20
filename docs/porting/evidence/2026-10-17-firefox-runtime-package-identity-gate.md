# Firefox runtime package identity gate

Date: 2026-10-17

## Scope

This bounded host-side gate prevents a QEMU WebRender run from using a Firefox
binary whose Debian package identity differs from the source snapshot covered
by the two virgl compatibility patches. It reads only `packages.lock` and the
source patch manifest. It does **not** prove that the runtime bytes contain
those patches; that remains the responsibility of the real package provenance
gate and a later QEMU WebRender run.

## Gate

Implementation:

```text
tools/riscv/debian/rootfs/firefox_compat/runtime_provenance_gate.py
```

Regression tests:

```text
tools/riscv/tests/test_firefox_runtime_provenance_gate.py
```

The focused test run passed:

```text
$ PYTHONPATH=. python3 -m unittest -q \
    tools.riscv.tests.test_firefox_runtime_provenance_gate
...
Ran 3 tests in ...
OK
```

The gate requires a single `firefox-esr` row with architecture `riscv64` and
the exact version in `source_patch_manifest.json`.

## Existing rootfs audit

Source manifest:

```text
tools/riscv/debian/rootfs/firefox_compat/source_patch_manifest.json
sha256=76d00144ea8ecaf9a776fbac74fc38d92a525ee35d5a46b1d37fa576f22fbfde
package=firefox-esr
version=140.16.0esr-1~deb13u1
```

The matching package lock passes:

```text
$ python3 tools/riscv/debian/rootfs/firefox_compat/runtime_provenance_gate.py \
    --packages-lock target/dev-overlays/browser-web/rootfs/packages.lock \
    --source-manifest tools/riscv/debian/rootfs/firefox_compat/source_patch_manifest.json
FIREFOX_RUNTIME_PROVENANCE PASS package=firefox-esr architecture=riscv64 version=140.16.0esr-1~deb13u1
```

The package-lock hash is
`f3f63d893702f7a1a97d2cd7fdcf063de475069f437cf8af25cefa875b4e910a`.

The current `browser-web-drm` lock is rejected, as required:

```text
$ python3 tools/riscv/debian/rootfs/firefox_compat/runtime_provenance_gate.py \
    --packages-lock target/debian-riscv/browser-web-drm/rootfs/packages.lock \
    --source-manifest tools/riscv/debian/rootfs/firefox_compat/source_patch_manifest.json
FIREFOX_RUNTIME_PROVENANCE FAIL reason=version mismatch: 153.4.0esr-1~deb13u1; expected 140.16.0esr-1~deb13u1
```

Its package-lock hash is
`77c72c2773e1d1d279b6424ba08b61ff0b71b6104a0a0fdb81fb56370d92b724`.

## Boundary

This closes the runtime package identity check for the matching development
overlay and records the existing DRM rootfs as ineligible for the ESR 140
source patches. It is not a patched Firefox build, not a WebRender result, and
not hardware evidence. The next Task 6 gate is still an authenticated ESR
RISC-V package build with both patches, followed by the strict QEMU scene gate
requiring `renderer=virgl`, pixels, frames, and restart recovery.
