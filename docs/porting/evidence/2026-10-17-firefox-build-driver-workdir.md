# Firefox build-driver work-volume gate

Date: 2026-10-17

## Scope

This is a host-side Task 6 build-entry check. It does not build Firefox and
does not claim a WebRender runtime result.

The build driver checks free space on `--output-dir` before creating its
disposable source copy. Its work directory is now created as
`$OUTPUT_DIR/.firefox-esr-build.XXXXXX`, so the multi-gigabyte source copy and
package output stay on the checked filesystem. The previous `${TMPDIR:-/tmp}`
default could bypass that check and consume the host root filesystem.

## Evidence

The following checks passed:

```text
python3 -m unittest -q tools.riscv.tests.test_firefox_build_driver
----------------------------------------------------------------------
Ran 4 tests in 0.002s

OK

bash -n tools/riscv/debian/rootfs/firefox_compat/build_firefox_esr.sh
git diff --check -- tools/riscv/debian/rootfs/firefox_compat/build_firefox_esr.sh tools/riscv/tests/test_firefox_build_driver.py
```

The test suite includes a regression assertion that the work-directory
template is rooted at `$OUTPUT_DIR` and that the old `/tmp` template is absent.
The immutable builder-image, source-package, zero-fuzz patch, and 12 GiB
free-space gates remain unchanged.

## Boundary

This closes the build-driver work-volume safety check only. A patched Firefox
package still requires the pinned builder image and its dependencies; a
passing package gate is still required before any QEMU WebRender receipt can
be accepted.
