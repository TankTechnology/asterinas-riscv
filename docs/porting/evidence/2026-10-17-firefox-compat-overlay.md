# Firefox compatibility package overlay gate

Date: 2026-10-17

## Scope

This adds the development-only handoff from a provenance-verified Firefox ESR
package to a disposable root directory. `compat_overlay.py` runs the package
provenance gate first, validates Debian data-archive member paths and staged
symlinks, requires the Firefox executable and `libxul.so`, then merges the
validated tree and writes a deterministic marker. It does not modify dpkg's
database and does not call this package a Debian-signed package.

## Verification

The tests create a minimal synthetic `riscv64` Debian archive and exercise the
real extraction and merge code:

```text
$ PYTHONWARNINGS=error PYTHONPATH=tools/riscv python3 -m unittest -q \
    tools.riscv.tests.test_firefox_compat_overlay
----------------------------------------------------------------------
Ran 3 tests in 0.367s

OK

$ ruff check \
    tools/riscv/debian/rootfs/firefox_compat/compat_overlay.py \
    tools/riscv/tests/test_firefox_compat_overlay.py
All checks passed!

$ PYTHONPATH=tools/riscv python3 -m unittest -q \
    tools.riscv.tests.test_firefox_compat_overlay \
    tools.riscv.tests.test_firefox_package_provenance_gate \
    tools.riscv.tests.test_firefox_source_patch_gate \
    tools.riscv.tests.test_firefox_drm_scene_gate \
    tools.riscv.tests.test_firefox_webrender_gate
----------------------------------------------------------------------
Ran 34 tests in 0.825s

OK
```

The matrix covers a successful merge, an escaping symlink, and a package
missing required runtime files. The failure cases leave their destination
root untouched. No real Firefox package or rootfs was changed.

## Result and boundary

**PASS:** a future built package now has a tested, explicit development-root
handoff after provenance verification. The next step is to feed a real
patched ESR package through this overlay in a disposable QEMU rootfs; the
Firefox WebRender gate must still prove `renderer=virgl`, pixels, frames, and
restart recovery.
