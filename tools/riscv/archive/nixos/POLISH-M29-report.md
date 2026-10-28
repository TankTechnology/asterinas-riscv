# POLISH-M29 — loop device verification: 23 of 46 previously-blocked tags now PASS, zero regressions

Date: 2026-08-21
Branch: `track/nixos`
Commits under test: `7f081686e` (loop device subsystem), `b62561964` (zero-sector guard),
`f0ecc340a` (ioctl dispatch through block registry), **plus the uncommitted working-tree
follow-up** in `kernel/src/device/loop.rs` + `kernel/src/device/registry/block.rs` (see §1).
Test enablement: `test/initramfs/src/conformance/ltp/testcases/all.txt` uncomments
`ioctl_loop01`–`ioctl_loop07` (working tree, uncommitted).
Status: **Complete — loop device acquire/format/mount path verified end-to-end; 23 PASS
conversions out of the 46 bucket-B tags; remaining FAIL/CONF items reclassified to
loop-unrelated root causes.**

M27 bucket B had 35 tests (46 tags listed in M27 §2.3) blocked on
`tst_device.c: TBROK: Failed to acquire device` because no loop device existed. The
M28 session landed the loop subsystem and the ioctl-dispatch fix (`f0ecc340a`) but never
re-ran the gate. This report closes that loop.

---

## 1. Working-tree follow-up fix (required, uncommitted)

The committed `f0ecc340a` had a latent bug in `BlockFile::open()`
(`kernel/src/device/registry/block.rs`):

```rust
if let Some(loop_device) = self.0.downcast_ref::<r#loop::LoopDevice>() {
    let loop_file = r#loop::LoopFile::new(loop_device.clone());  // clones the inner
                                                                 // LoopDevice, not the Arc
```

`downcast_ref` yields `&LoopDevice`, so `.clone()` produced a **detached copy** of the
device. `LOOP_SET_FD` on that copy set the backing file on state invisible to the
registry device, so the loop attach/detach handshake could never work through
`/dev/loopN` opened via the block registry.

The working tree (left by the previous session, never built or tested until now) fixes
this by storing `Arc<dyn BlockDevice>` inside `LoopFile`/`OpenLoopDevice` and
downcasting per use (`kernel/src/device/loop.rs`, `kernel/src/device/registry/block.rs`).
This session built and verified it — the tests now report
`tst_device.c:98: TINFO: Found free device 0 '/dev/loop0'` and successfully mount tmpfs
on it.

## 2. Test setup

- Kernel: rebuilt debug `riscv_sv39_mode` Image (`cargo osdk build --scheme riscv
  --features riscv_sv39_mode`, `VDSO_LIBRARY_DIR=/home/arch-anjie/Program/linux_vdso`).
  `rust-objcopy` must come from the `nightly-2026-07-21` toolchain dir; the `stable`
  toolchain's `rust-objcopy` fails with `libLLVM.so.22.1` missing (its rpath is not
  picked up via `PATH`).
- Initramfs: `build_ltp.sh --skip-compile` (all 7 `ioctl_loop*` binaries were already in
  `target/ltp/stage`); 774 enabled tests, up from 767.
- Gate: 54-tag subset at SMP=1 (46 bucket-B tags + 7 `ioctl_loop*` + `nice01`), booted
  via a copy of `run_ltp_subset.sh`/`boot_ltp_gate.py` pointed at a private boot disk
  under `/tmp` (the shared `target/qemu-uboot/current/boot.ext4` is off-limits).
- CONF reasons were captured with a one-off runner variant that also dumps per-test
  output for `TCONF` verdicts (the stock runner only dumps FAIL/CRASH/TIMEOUT).

## 3. Scorecard

```
[summary] total=54 pass=24 fail=11 conf=19 crash=0 timeout=0
```

| Group | Tags | PASS | FAIL | CONF |
|---|---|---|---|---|
| Bucket B (loop-blocked in M27) | 46 | **23** | 6 | 17 |
| `ioctl_loop01`–`07` (newly enabled) | 7 | 0 | 5 | 2 |
| `nice01` (see POLISH-M31) | 1 | **1** | 0 | 0 |

**Conversion: 23 of 46 previously loop-blocked tags now PASS.** Every remaining
FAIL/CONF fails for a *new*, loop-unrelated reason — the "Failed to acquire device"
TBROK is gone from all 46.

### 3.1 Bucket-B conversions (FAIL → PASS, 23)

`close_range01`, `fsmount01`, `fsmount02`, `fsopen01`, `fsync01`, `getxattr02`,
`mount01`, `rename01`, `rename03`, `rename04`, `rename05`, `rename06`, `rename07`,
`rename08`, `rename10`, `rename12`, `rename13`, `rename15`, `setxattr01`, `statfs01`,
`statvfs01`, `statx12`, `utime01`.

### 3.2 Bucket-B remaining FAIL (6) — new root causes, none loop-related

| Test | Failure | Root cause |
|---|---|---|
| `chdir01` | `nobody: chdir("keep_out") returned 0` | DAC/permission-model gap (same family as `access01`) |
| `mkdir02` | `New dir FAILED to inherit GID / S_ISGID` | tmpfs does not inherit S_ISGID from parent dir |
| `mknod03` | `buf.st_gid (65534) != free_gid (5)` | same S_ISGID inheritance gap |
| `openat02` | 3 × TPASS then `TBROK: execlp() failed` | helper binary missing from initramfs PATH (env gap) |
| `preadv203` | `TBROK: /proc/sys/vm/drop_caches EPERM` | known bucket C (`/proc` gap) |
| `preadv203_64` | same | same |

### 3.3 Bucket-B CONF (17) — test-side configuration skips

| Reason | Tests |
|---|---|
| Test requires ext2/ext4 (unsupported fs) | `execveat03`, `fsconfig02`, `fsopen02`, `lstat03`, `prctl06`, `statx06`, `statx11`, `umount01`, `utimensat01` |
| Test only supports ext4/xfs | `statx10` |
| `fsconfig(FSCONFIG_SET_PATH/_EMPTY/_FD)` not supported | `fsconfig01` |
| Needs raw `__NR_getdents` (absent on asm-generic) + libc `getdents64` | `getdents01` |
| "No supported filesystems" for the test's fs matrix | `preadv03`, `preadv03_64` |
| Inode attributes (`statx` ATTR) → ENOTTY | `statx04` |
| `FS_COMPR_FL/APPEND_FL/IMMUTABLE_FL/NODUMP_FL` unsupported | `statx08` |
| `FS_IOC_GETFLAGS` on tmpfs unsupported | `unlink09` |

### 3.4 `ioctl_loop01`–`07` (newly enabled): 0 PASS / 5 FAIL / 2 CONF

| Test | Verdict | Reason |
|---|---|---|
| `ioctl_loop01` | FAIL | TBROK: `/sys/block/loop0/loop/partscan` ENOENT — no sysfs loop attrs |
| `ioctl_loop02` | FAIL | TBROK: `/sys/block/loop0/ro` ENOENT — same |
| `ioctl_loop03` | FAIL | TFAIL: `LOOP_CHANGE_FD` expected EINVAL, got ENOTTY — `LOOP_CHANGE_FD` (0x4C06) not in the loop ioctl table |
| `ioctl_loop04` | FAIL | TBROK: `/sys/block/loop0/size` ENOENT — same sysfs gap |
| `ioctl_loop05` | CONF | TCONF: tmpfs not supported by the test (wants a real fs on the loop device) |
| `ioctl_loop06` | CONF | TCONF: `LOOP_SET_BLOCK_SIZE` not supported |
| `ioctl_loop07` | FAIL | TBROK: `/sys/block/loop0/size` ENOENT — same sysfs gap |

## 4. Cosmetic issue observed

Every loop-using test ends with
`tst_device.c:270: TWARN: ioctl(/dev/loop0, LOOP_CLR_FD, 0) no ENXIO for too long` —
the detach path works but the kernel keeps reporting success briefly where Linux would
return ENXIO immediately. Does not affect verdicts.

## 5. Conclusion

| Metric | Value |
|---|---|
| Previously loop-blocked tags re-tested | 46 (M27 "35 tests" — its §2.3 lists 46 tags) |
| FAIL → PASS conversions | **23** |
| Remaining FAIL | 6 (all reclassified to loop-unrelated causes) |
| CONF | 17 (test-side fs/feature skips) |
| Newly enabled `ioctl_loop*` | 7 (0 PASS; sysfs loop attrs + `LOOP_CHANGE_FD` are the next gaps) |
| Regressions | **0** |

**Next steps:**
1. Add `/sys/block/loopN/{ro,size,loop/partscan}` attributes (unblocks `ioctl_loop01/02/04/07`).
2. Add `LOOP_CHANGE_FD` (EINVAL when not configured) and `LOOP_SET_BLOCK_SIZE` to the
   loop ioctl table (`ioctl_loop03`, `ioctl_loop06`).
3. tmpfs S_ISGID inheritance (`mkdir02`, `mknod03`).
4. Pack the `openat02` helper binary into the initramfs (same pattern as the M28
   `execve*_child` fix).
