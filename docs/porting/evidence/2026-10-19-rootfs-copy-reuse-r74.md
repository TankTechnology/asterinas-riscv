# Rootfs snapshot reuse evidence (r74)

- Gate: `rootfs-copy-snapshot-reuse`
- Scope: host-side preparation only; no QEMU launch and no hardware access.
- Date: 2026-10-19 (Asia/Shanghai)
- Status: `pass`

## Change

`ConcreteOperations.prepare()` now passes the already verified
`snapshot_inputs()["root_image"]` digest to `copy_sparse_root()`.  The copy
helper validates the supplied lowercase SHA-256 shape, still hashes the source
after `cp` and hashes the copied image, and rejects any mismatch.  Calls that
do not have a snapshot retain the previous behavior and hash the source before
copying.

This removes one redundant full source-image hash from each preparation while
keeping mutation-during-copy and copied-content checks fail-closed.

## Verification

| Check | Result |
| --- | --- |
| `make test_riscv_debian_rootfs_unit` | PASS, 327/327 |
| `bash tools/riscv/firefox_fast_check.sh` | PASS (`FIREFOX_FAST_CHECK_PASS`, 327 tests) |
| targeted snapshot-reuse and malformed-digest tests | PASS, 2/2 |
| `python3 -m compileall -q tools/riscv/debian/rootfs tools/riscv/tests` | PASS |
| `bash -n tools/riscv/debian/rootfs/*.sh tools/riscv/*.sh` | PASS |
| `git diff --check` | PASS |
| QEMU / Firefox / physical board | NOT RUN |

## Remaining boundary

The gate still creates a writable raw copy for QEMU and validates its final
digest.  A content-addressed qcow2 overlay would be a separate design change;
it is not enabled by this receipt.
