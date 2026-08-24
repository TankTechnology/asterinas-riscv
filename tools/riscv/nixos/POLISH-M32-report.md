# POLISH-M32 — loop ioctl_loop* cleared (5 PASS / 2 env-CONF), chdir01 + access01 DAC fix, tmpfs S_ISGID inheritance

Date: 2026-08-22
Branch: `track/nixos`
Commits:
- `f0e9925ef` feat(kernel): complete loop device semantics for ioctl_loop01-07
- `027c9893a` fix(kernel): enforce search permission on chdir target; DAC_OVERRIDE covers directories
- `4f01021b9` fix(kernel): inherit parent gid and S_ISGID on ramfs/tmpfs inode creation
- `81553d95a` test(ltp): pack openat02_child helper and enable busybox cp applet
Status: **Complete — all four M31 follow-ups landed; 12 new PASS across the four buckets; zero regressions on the bucket-B regression gate.**

---

## 1. Loop device: `ioctl_loop01`–`07` (FAIL → 5 PASS / 2 CONF)

M31 left the newly-enabled `ioctl_loop*` tests at 0 PASS / 5 FAIL / 2 CONF. The M32
kernel change completes the loop ioctl surface and adds the sysfs attributes the tests
read (all field offsets verified against the riscv64 cross sysroot's `linux/loop.h`):

- **`LoopDevice` state**: `lo_flags` (READ_ONLY/AUTOCLEAR/PARTSCAN/DIRECT_IO),
  `lo_sizelimit`, backing file path (for the sysfs attribute).
- **ioctls**: `LOOP_SET_STATUS(64)` now apply the Linux flag rules (only
  AUTOCLEAR/PARTSCAN settable, only AUTOCLEAR clearable; legacy SET_STATUS resets
  `lo_sizelimit`); `LOOP_GET_STATUS(64)` report flags/sizelimit/name (fixing the legacy
  `loop_info` offsets — `lo_flags` @32, `lo_name` @36, previously misread at 28);
  `LOOP_CHANGE_FD` (EINVAL unless bound + read-only + same-size backing);
  `LOOP_SET_CAPACITY` (re-read size from the backing file); `LOOP_SET_BLOCK_SIZE` and
  `LOOP_CONFIGURE` (block size validated: power of two in 512..4096, else EINVAL;
  `LOOP_CONFIGURE` attaches atomically and returns EBADF for a bad fd, EBUSY when
  bound).
- **Read-only enforcement**: `LOOP_SET_FD` on a file not opened for writing marks the
  device read-only; `write(2)` then fails with EROFS.
- **`LOOP_CLR_FD` on an unbound device returns ENXIO** (Linux semantics). This also
  eliminates the `tst_device.c: TWARN: LOOP_CLR_FD no ENXIO for too long` warning that
  made the runner classify otherwise-green tests as FAIL (nonzero exit).
- **sysfs**: `/sys/block/loopN/{ro,size}` and
  `/sys/block/loopN/loop/{backing_file,partscan,autoclear,sizelimit}` via the systree
  infrastructure (`BlockSysNode` branch + per-device branch + `loop` leaf node).

### Results (SMP=1)

| Test | M31 | M32 | Notes |
|---|---|---|---|
| `ioctl_loop01` | FAIL | **CONF** | all sysfs/ioctl checks pass; TCONF only because `parted` is missing (partition-scan subcheck skipped) |
| `ioctl_loop02` | FAIL | **PASS** | RO via SET_FD and LOOP_CONFIGURE, CHANGE_FD both directions |
| `ioctl_loop03` | FAIL | **PASS** | CHANGE_FD on RW device → EINVAL |
| `ioctl_loop04` | FAIL | **PASS** | SET_CAPACITY + `size` sysfs attr |
| `ioctl_loop05` | CONF | **CONF** | test requires a real fs on the loop device (tmpfs unsupported by test) |
| `ioctl_loop06` | CONF | **PASS** | SET_BLOCK_SIZE / CONFIGURE block-size validation |
| `ioctl_loop07` | FAIL | **PASS** | SET_STATUS64 sizelimit clamping + CONFIGURE |

**Regression gate:** the 46-tag M27 bucket-B subset is unchanged at
23 PASS / 6 FAIL / 17 CONF — zero regressions from the loop changes.

## 2. chdir01 / access01 — DAC permission model (FAIL → PASS)

Two distinct kernel bugs, one commit (`027c9893a`):

1. **`sys_chdir` never checked search permission on the target directory.**
   `PathResolver::lookup_at_path` checks MAY_EXEC only on intermediate components, so
   `chdir("keep_out")` (a 0644 directory) succeeded for `nobody` instead of returning
   EACCES. `sys_chdir` now checks `Permission::MAY_EXEC` on the resolved target.
2. **`Inode::check_permission` applied the "exec overridable only if an exec bit is
   set" rule to directories.** Linux's `generic_permission()` restricts that rule to
   regular files; with CAP_DAC_OVERRIDE, directory search/read/write is always
   overridable. Without this fix, root could not even walk into a directory without
   execute bits (access01's root-side `access("accessdir_r/...")` failed with EACCES).

`setresuid`/`seteuid` already drop the effective capset correctly (verified in
`credentials_.rs`), so no changes were needed there.

### Results (SMP=1)

`chdir01`, `access01`, `access02`, `access03`, `fchdir01`: **5/5 PASS**.

A 15-test permission/exec regression subset (`chmod01 chown01 creat01 execve01
execve02 execveat01 fchmod01 lstat01 mknod01 open01 openat01 readlink01 rename01
rmdir01 stat01`) shows no regressions (`execve02`/`execveat01` TBROK on the missing
busybox `cp` applet — a pre-existing environment gap fixed separately in §4).

Residual DAC gaps (not exercised by these tests): `access()` still checks fsuid/fsgid
rather than real uid/gid (matters only for setuid-without-setreuid flows), the group
permission check ignores supplementary groups, and the S_ISGID strip rule for
non-privileged group-executable file creation is not implemented.

## 3. tmpfs S_ISGID inheritance: mkdir02 / mknod03 (FAIL → PASS)

ramfs (and therefore tmpfs, which is ramfs-based) assigned every new inode the
creator's fsuid/fsgid and ignored the parent directory's S_ISGID bit. `mknod`,
`create`, and `create_tmpfile` now follow Linux's `inode_init_owner()`: when the
parent directory has S_ISGID set, the child inherits the parent's gid, and a new
directory also inherits S_ISGID itself.

### Results (SMP=1)

`mkdir02`, `mknod03`: **PASS**; `mknod01`/`mknod02` unchanged (no regression).

## 4. Test-environment packaging: busybox `cp` + `openat02_child` (`81553d95a`)

- `build_busybox.sh`: enable the `cp` applet (LTP helpers shell out to `cp`).
- `build_ltp.sh`: pack `openat/openat02_child` into the initramfs (same pattern as the
  M28 `execve*_child` fix) and symlink `cp` into `/bin`.

### Results (SMP=1)

| Test | Before | After | Notes |
|---|---|---|---|
| `execve02` | FAIL (TBROK: `cp` not found) | **PASS** | |
| `execve05` | FAIL (TBROK) | **PASS** | |
| `execveat01` | FAIL (TBROK) | **PASS** | |
| `execveat02` | FAIL (TBROK) | **PASS** | |
| `execve04` | FAIL (TBROK) | FAIL | now surfaces a **pre-existing kernel gap**: `execve_child shouldn't be executed` — an exec that should have failed succeeded |
| `openat02` | FAIL (TBROK: execlp) | FAIL | now surfaces the **pre-existing O_NOATIME gap**: `openat02.c:205 TFAIL: test O_NOATIME for openat failed` |

Both remaining FAILs are genuine kernel feature gaps that were previously masked by
the environment failures. O_NOATIME needs the open-time owner/CAP_FOWNER check plus
atime-suppression plumbing through the per-open-file read path — deliberately left
out of this batch.

## 5. Conclusion

| Metric | Value |
|---|---|
| New PASS | 12 (`ioctl_loop02/03/04/06/07`, `chdir01`, `access01`, `mkdir02`, `mknod03`, `execve02`, `execve05`, `execveat01/02`) + `access02/03`, `fchdir01` verified PASS |
| Newly surfaced genuine gaps | 2 (execve04 exec-should-fail, O_NOATIME via open02/openat02) |
| Regressions | **0** (46-tag bucket-B gate unchanged; 15-test permission/exec subset clean) |
| Environment-bound CONF | `ioctl_loop01` (no parted), `ioctl_loop05` (test requires real fs) |

**Next steps:**
1. O_NOATIME (open02, openat02): open-time owner/CAP_FOWNER check + atime plumbing.
2. execve04: investigate why an exec expected to fail succeeds (per-binary analysis).
3. access01 residual semantics: real-uid checking for access(2) without AT_EACCESS,
   supplementary-group membership in `check_permission`.
4. Full 774-test gate re-run to re-baseline the scorecard.
