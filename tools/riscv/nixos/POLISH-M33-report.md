# POLISH-M33 — full-gate re-baseline + ETXTBSY, access(2) real-ID semantics, O_NOATIME

Date: 2026-08-22
Branch: `track/nixos`
Commits:
- `823b02848` fix(kernel): deny execve of files open for writing (ETXTBSY)
- `66ba0abff` refactor(kernel): drop unused LoopFile::device helper
- `2e6604a42` fix(kernel): access(2) checks real IDs; permission checks honor supplementary groups
- `b1f8b1156` fix(kernel): implement O_NOATIME (open-time permission + atime suppression)
- `9ff7989e9` fix(kernel): allow NULL timespec in clock_getres
- (report commit)
Status: **Complete — full 774-test gate re-baselined; execve04, open02, openat02 fixed; access(2) real-ID semantics verified by raw probe.**

---

## 1. Full-gate re-baseline (774 tests, SMP=1)

```
as-run (kernel b1f8b1156):  total=774 pass=592 fail=71 conf=101 timeout=5 crash=0
after 9ff7989e9:            total=774 pass=592 fail=70 conf=102 timeout=5 crash=0
```

Methodology notes:

- The gate was booted on a private `/tmp` disk (the shared
  `target/qemu-uboot/current/boot.ext4` is off-limits). The main run covered 684 tests
  before the harness VM was torn down by a host-side session restart; the remaining 90
  tags were re-run as a subset with the identical kernel Image and initramfs. Verdicts
  were stitched.
- The 5 TIMEOUTs: `epoll01`, `fcntl14`, `fcntl14_64` are the known TCG fork-perf
  artifacts (unchanged since M10); `preadv203`/`preadv203_64` were host-contention
  flakes — both deterministically FAIL in isolation on the known
  `/proc/sys/vm/drop_caches` gap (M27 bucket C).
- **Zero regressions**: every remaining FAIL is a previously classified M27 bucket
  item. `clock_getres01` (FAIL in the gate) was corrected after the run — see §5.

### Delta vs the M25 baseline (649 tests: 510 PASS / 81 FAIL / 58 CONF / 3 CRASH)

The test count grew to 774 (M27 batch-3 expansion + M29 `ioctl_loop01`–`07`
enablement). PASS count rose from 510 to 592 (+82). Previously-failing tests that now
pass: 23 loop-blocked bucket-B conversions (M29), `nice01` (M31),
`ioctl_loop02/03/04/06/07` (M32), `chdir01`, `access01`, `mkdir02`, `mknod03` (M32),
`execve02`, `execve05`, `execveat01`, `execveat02` (M32 environment fix), and
`execve04`, `open02`, `openat02` (M33).

### Newly surfaced gaps

None unclassified. The two gaps surfaced by the M32 environment fixes (`execve04`'s
missing ETXTBSY, O_NOATIME via `open02`/`openat02`) were both fixed in M33 (§2, §4).

## 2. execve04 — ETXTBSY for executables open for writing (FAIL → PASS)

**Root cause.** The kernel had no `i_writecount` equivalent: `execve(2)` succeeded even
while another process held the file open for writing, so `execve04`'s child
(`execve_child`) executed and reported TFAIL from its `main`.

**Fix** (`823b02848`): a per-inode `WriteAccessTracker` (Linux `i_writecount`
semantics, pre-6.11 — matching the 5.13 version we report):

- `kernel/src/fs/vfs/fs_apis/inode.rs`: new `Extension` group3 slot.
- `kernel/src/fs/vfs/fs_apis/inode_ext.rs`: `WriteAccessTracker`
  (acquire/release/deny/allow with ETXTBSY in both directions) and the RAII
  `WriteAccessDenyGuard`.
- `kernel/src/fs/file/inode_handle.rs`: count write opens of regular files at
  `InodeHandle::new_unchecked_access` and release on `Drop`.
- `kernel/src/process/execve.rs`: `do_execve` holds a deny guard while the program is
  built and loaded; the count is restored even on error paths.

Not implemented (Linux behavior, not exercised by the gate): `MAP_SHARED|PROT_WRITE`
mmaps also count as write access.

**Verification (SMP=1):** `execve04` PASS; `execve02/05`, `execveat01/02` stay PASS;
16-test write/exec regression subset clean (13 PASS / 2 pre-existing FAIL: open02,
openat02 at the time / 1 CONF).

## 3. access(2) real-ID semantics + supplementary groups (`2e6604a42`)

**Gaps.** `do_faccessat` always checked against fsuid/fsgid, but `access(2)` (and
`faccessat2` without `AT_EACCESS`) must check the **real** UID/GID. Separately, the
group permission class only matched the (fs)gid, never supplementary groups.

**Fix.** `Inode::check_permission` is now a thin wrapper over a UID/GID-parameterized
helper; `check_permission_with_real_ids` uses ruid/rgid (capability checks still use
the effective set, as in Linux). The group class matches when the inode's group is the
fsgid **or any supplementary group** (`in_group_p`). `do_faccessat` picks the variant
based on `AT_EACCESS`.

**Verification.** Raw-syscall probe (`tools/riscv/nixos/ltp/access_probe.c`, run as
bare-initramfs `/init`), 5/5 PASS:

- `access(R_OK)` on a 0600 root-owned file succeeds with ruid=root, euid=nobody
- `faccessat2(AT_EACCESS)` on the same file fails with EACCES (effective uid=nobody)
- `open()` on it still fails with EACCES (fsuid semantics unchanged)
- `open()` and `access()` on a 040 group-5 file succeed for nobody with group 5 as a
  supplementary group

LTP: `access01/02/03`, `chdir01`, `fchdir01` stay PASS; permission regression subset
(`chmod01 chown01 open01 openat01 stat01 mknod02 mkdir02`) 12/12 PASS.

## 4. O_NOATIME (`b1f8b1156`) — open02 / openat02 (FAIL → PASS)

The M32 plan estimated this as a large change, but the `Inode::read_at` status-flags
plumbing already existed end-to-end; only the consumers were missing:

1. **Open-time permission** (`open02`): `InodeHandle::new` rejects `O_NOATIME` with
   EPERM unless the caller's fsuid owns the file or holds `CAP_FOWNER` (new
   `Inode::check_noatime_permission`; the `has_capability` helper was generalized from
   the DAC_OVERRIDE path).
2. **Atime suppression** (`openat02`): ramfs `read_at` skips the atime update when the
   open flags contain `O_NOATIME` (covers tmpfs).

Residual gap: other filesystems (ext2/exfat/virtiofs) still ignore the flag in their
read paths — a one-line change each, deferred until tests need them.

**Verification (SMP=1):** `open02`, `openat02` PASS; `open01`, `openat01`, `read01`,
`write01`, `access01`, `chdir01` stay PASS.

## 5. clock_getres01 — post-gate correction of the M28 fix (`9ff7989e9`)

The gate flagged `clock_getres01` FAIL. Root cause: the M28 fix (`724b2440a`)
implemented "NULL timespec → EFAULT", but that is **not** Linux semantics — verified
against host Linux, `clock_getres(2)` with a NULL `res` skips the copy-out and returns
0 (LTP has dedicated NULL-res variants expecting success). Invalid clock IDs still
return EINVAL first. Fix verified: `clock_getres01` FAIL → CONF (the only remaining
non-passing subcases are TCONF for the unsupported `*_ALARM` clocks).

## 6. Conclusion

| Metric | Value |
|---|---|
| Full-gate baseline (774 tests, SMP=1) | **592 PASS / 70 FAIL / 102 CONF / 5 TIMEOUT / 0 CRASH** (post-`9ff7989e9`; as-run 592/71/101/5/0) |
| M33 fixes | `execve04`, `open02`, `openat02` FAIL → PASS; `clock_getres01` FAIL → CONF; access(2) real-ID + supplementary groups aligned (probe-verified) |
| Regressions | **0** — every remaining FAIL is a previously classified M27 bucket item |

**Next steps:**
1. Re-run the full gate after future fix batches to keep the baseline fresh.
2. O_NOATIME for ext2/exfat/virtiofs read paths.
3. `preadv203`/`madvise06`: `/proc/sys/vm/drop_caches`.
4. clock batch: `clock_settime01` (ENOSYS), `clock_nanosleep02` (TCG timing),
   `clock_gettime03/04` (/proc ns files).
