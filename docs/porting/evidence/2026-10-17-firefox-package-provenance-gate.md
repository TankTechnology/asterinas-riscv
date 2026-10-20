# Firefox package provenance admission gate

Date: 2026-10-17

## Scope

This is the next bounded step after the source patch gate. It defines and
tests the admission contract for a future Firefox ESR RISC-V package built with
the two audited compatibility patches. The gate checks package bytes, Debian
control identity, the source-patch manifest hash, both patch hashes, and a
recorded source/build identity. It never extracts or installs a package.

No real Firefox package was available in this run, so this receipt does not
claim a Firefox build, a rootfs image, a QEMU runtime result, WebRender, or
hardware acceleration.

## Artifacts

- Gate: `tools/riscv/debian/rootfs/firefox_compat/package_provenance_gate.py`
- Tests: `tools/riscv/tests/test_firefox_package_provenance_gate.py`
- Source manifest consumed by the gate:
  `tools/riscv/debian/rootfs/firefox_compat/source_patch_manifest.json`

The required provenance envelope contains an exact package name, architecture,
version and SHA-256; the source manifest path and SHA-256; the two patch paths
and hashes; and a source `.dsc` hash plus immutable builder image digest. The
gate also delegates full source-manifest shape validation to the source patch
gate, so a package cannot be admitted with a shortened or altered patch list.

## Verification

The tests build a minimal synthetic `riscv64` Debian archive with `dpkg-deb`,
then exercise the real gate:

```text
$ PYTHONPATH=tools/riscv python3 -m unittest -q \
    tools.riscv.tests.test_firefox_package_provenance_gate
----------------------------------------------------------------------
Ran 5 tests in 0.398s

OK

$ ruff check \
    tools/riscv/debian/rootfs/firefox_compat/package_provenance_gate.py \
    tools/riscv/tests/test_firefox_package_provenance_gate.py
All checks passed!

$ PYTHONPATH=tools/riscv python3 -m unittest -q \
    tools.riscv.tests.test_firefox_package_provenance_gate \
    tools.riscv.tests.test_firefox_source_patch_gate \
    tools.riscv.tests.test_firefox_drm_scene_gate \
    tools.riscv.tests.test_firefox_webrender_gate
----------------------------------------------------------------------
Ran 31 tests in 0.479s

OK
```

The test matrix includes package hash drift, Debian metadata drift, patch hash
drift, and direct CLI success. All package and source inputs are temporary;
the repository and audited source snapshot remain unchanged.

## Result and boundary

**PASS:** the future patched package has a fail-closed, independently tested
admission point. The next required step is to obtain the matching Debian ESR
source package and a reproducible RISC-V builder, produce the patched `.deb`,
record its real hashes, and run this gate before any rootfs overlay or QEMU
scene test.
