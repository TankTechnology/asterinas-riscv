# Storage cleanup audit r119

This is a pre-delete audit for the short Firefox `user-recv` probe.  The
workspace was below the required 60 GiB free-space floor, so only a generated
artifact with no active management-chain reference was considered.

| Path | Size | Classification | Decision | Evidence |
|---|---:|---|---|---|
| `target/e3-render-policy-r1/rootfs-r8/debian-root.ext2` (and its sibling metadata) | 2.0 GiB image, 2.0 GiB directory | generated failed E3/rootfs-r8 input | delete | no receipt or active gate references; current Firefox image is `/mnt/mydisk/asterinas-firefox-rootfs-20261019-preseed/debian-root.ext2` |
| `target/debian-riscv/desktop-m4-x11` | 1.0 GiB rootfs directory | superseded desktop M4 input | delete | no receipt JSON reference; retained evidence is documentation only |
| `target/debian-riscv/desktop-m4-cached-20261017-r2` | 1.0 GiB rootfs directory | superseded cached M4 input | delete | no receipt JSON reference; current probe uses `/mnt/mydisk/asterinas-firefox-rootfs-20261019-preseed` |

The image is not tracked by Git, is not referenced by the current E1-E5
management chain, and is not used by a running QEMU process.  No source,
manifest, evidence receipt, or `/mnt/mydisk` artifact is removed by this
cleanup.

## Result

The two additional superseded rootfs directories were removed through the
root-owned persistent development container.  The resulting free space is
64,342,904,832 bytes (`df -h`: 60G) on `/mnt/shared`; `/mnt/mydisk` has
2.3T free.  No QEMU process was started.

Static and unit checks after deletion:

* `test_debian_browser_web`: 94 passed
* `test_debian_dev_overlay`: 24 passed
* `test_refactor_reference_scan`: 3 passed
* `test_browser_daily_use_gate`: 62 passed
* `test_megrez_firefox_daily_use`: 16 passed
* `test_megrez_debug`: 65 passed
* `test_megrez_debug_desktop`: 9 passed
* `test_megrez_gmac_gate`: 33 passed (the test suite's expected CLI-error cases are
  printed while the suite exits successfully)
* `compileall tools/riscv/debian/rootfs tools/riscv/tests`: passed
* reference scan and manifest verification: 206 candidates, passed
* `git diff --check`: passed

The full rootfs test collection remains environment-limited by the host's
missing `/usr/local/qemu/bin/qemu-img`: `test_debian_rootfs` ran 229 tests,
with 228 passing and one environment-only error in
`test_qcow2_root_overlay_is_small_and_binds_raw_backing`.  It was not used as
a reason to remove any additional artifact.

