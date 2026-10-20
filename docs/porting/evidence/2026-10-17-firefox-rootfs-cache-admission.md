# Firefox rootfs cache-admission gate

Date: 2026-10-17 (Asia/Shanghai)

The Firefox Task 6 rootfs build was being stopped by a stale apt archive before
the builder could determine whether that archive belonged to the frozen root.
`admit_downloaded_packages()` now reads `Package`, `Architecture`, and
`Version` from each `.deb` first. An archive whose identity is not in
`packages.lock` is skipped as an obsolete cache entry. An archive that is in
the lock still must match the authenticated `Packages` index by SHA-256, and
the archive control metadata is checked against the authenticated row.

This preserves the fail-closed behavior for installed or admitted packages;
it only prevents an old, no-longer-installed cache entry from aborting a
subsequent build.

## Verification

The focused regression tests pass:

```text
test_cache_admission_skips_superseded_archive_before_hash_lookup ... OK
test_cache_admission_still_rejects_installed_hash_missing_from_index ... OK
----------------------------------------------------------------------
Ran 2 tests ... OK
```

The full `DebianRootfsBuilderTests` class passes (`35 tests`), and the complete
`tools.riscv.tests.test_debian_rootfs` module passes (`214 tests`; the argparse
usage printed during the run is expected output from a tested command-line
failure path). `bash -n tools/riscv/debian/rootfs/build_rootfs.sh` and
`git diff --check` also pass.

This is a build-integrity prerequisite only. No rootfs was rebuilt in this
step because the host currently has about 3.3 GiB free on `/` and about 43 MiB
free on `/mnt/shared`, below the builder's recorded 12 GiB minimum. No Firefox
WebRender runtime claim is made from this gate.
