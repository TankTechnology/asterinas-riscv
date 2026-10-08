---
date: 2026-08-17
mode: diff
base: 9f71bdf5a
head: 715b2c541
branch: track/nixos
title: "\"POLISH-M27 code review \u2014 track/nixos full diff vs main\""
---

# Summary

This review covers the full diff of the `track/nixos` branch (96 commits, ~78 Rust source files in `kernel/` and `src/`) against `main`. The branch spans a wide range of work: virtio-sound/ALSA, cgroup v2, seccomp, System V IPC, multiple syscall implementations, and LTP test suite expansion.

**What the code does well**: The kernel changes are conservative and well-scoped — most new syscalls are minimal stubs or no-ops that unblock userspace without introducing complex logic. System call boundary validation is consistently applied. No `unsafe` blocks appear in any new kernel code. The commit history is dense and well-organized, with each commit scoped to a single feature or fix.

**Top issues by severity**:

1. **`fanotify.rs` — TOCTOU in `remove_mark` (major)**: The `remove_mark` function acquires the `marks` lock to find an index, drops it, then re-acquires to remove at that index. Between the two acquisitions, the vector can be mutated by another thread, making the index stale — potentially removing the wrong entry or panicking on an out-of-bounds index.

2. **`sound.rs` — unbounded allocation in `writei` (major)**: The PCM `writei` handler allocates `vec![0u8; len]` where `len` derives from user-supplied `xferi.frames` with no upper bound. A malicious user can trigger a multi-GB kernel heap allocation that exhausts memory and panics the kernel.

3. **`sound.rs` — log-levels (minor)**: `warn!` is used for normal PCM/CTL ioctl dispatch logging, flooding the log at WARN level during normal operation. Should be `debug!`.

4. **`fanotify.rs` — TOCTOU in `add_mark` (minor)**: The `add_mark` function checks for existing marks under the lock, drops it, then re-acquires to push. A concurrent thread could create a duplicate entry.

5. **`exception.rs` — TOCTOU in signal disposition (minor)**: `generate_fault_signal` reads the signal disposition under the lock, drops it, then re-acquires to call `set_default`. A concurrent `sigaction` could install a user handler that gets overwritten.

6. **`seccomp.rs` — missing re-entry check (minor)**: `SECCOMP_SET_MODE_STRICT` does not check whether the thread is already in a seccomp mode, allowing a thread in filter mode to be downgraded to strict mode. Linux returns `EINVAL` in this case.

7. **`shm.rs` — orphaned mapping on partial failure (minor)**: `shm_attach` establishes the mapping before recording the attachment. If the attachment record fails, the mapping is orphaned in the caller's address space.

**Structural recommendations**:

- The `class Boot` QEMU serial driver is duplicated across ~21 smoke-test Python scripts — extract it into a shared module.
- `boot_mlock_smoke.py` was copy-pasted from `boot_shm_smoke.py` without updating most references — a systematic cleanup pass is needed.
- Two commits (`eff751473`/`7e197038f` and `3be555c8e`/`d9a5759c7`) are duplicate pairs that should be squashed via interactive rebase.
- The documentation persona noted that no `.scml` files or linux-compatibility doc pages were updated despite many new syscalls being added — this is a systemic documentation gap, not a per-file defect.
- No security or hardware defects were found.

## Maintainability

### `commit a568fe0a9 message`

`imperative-subject` (minor): The commit message body for `a568fe0a9` ends with `🤖 Generated with [Claude Code](https://claude.com/claude-code)` — a generated-code footer that does not belong in a commit message.

**Fix.** Remove the generated-code footer from the commit message body.

### `commit d9a5759c7 message`

`atomic-commits` (major): Commits `3be555c8e` and `d9a5759c7` both fix the CLONE_BACKWARDS clone arg swap. `d9a5759c7` is a merge-conflict artifact — it has the same `kernel/src/syscall/clone.rs` diff but is missing the `tls_repro.c` fix that `3be555c8e` includes, and its commit message body contains a `# Conflicts:` marker.

**Fix.** Drop `d9a5759c7` via interactive rebase. The complete fix is already in `3be555c8e`.

### `commit d9a5759c7 message`

`imperative-subject` (minor): The commit message body for `d9a5759c7` contains a merge-conflict marker (`# Conflicts:`) that was not cleaned up before committing.

**Fix.** Remove the `# Conflicts:` line and the following `# tools/riscv/nixos/m4/tls_repro.c` line from the commit message body.

### `commit eff751473 message`

`atomic-commits` (major): Commits `eff751473` and `7e197038f` are duplicates — both contain the identical change to `kernel/src/thread/exception.rs` (force-deliver synchronous fault signals when blocked). The same 31-line diff appears twice in the history.

**Fix.** Drop the duplicate commit (`7e197038f`) via interactive rebase. Keep only the first occurrence (`eff751473`).

### `tools/riscv/nixos/mlock/boot_mlock_smoke.py` line 4

`dry` (major): `boot_mlock_smoke.py` was copy-pasted from `boot_shm_smoke.py` and many references were not updated: the docstring says "Boot the System V shared-memory smoke test" (should be "mlock/munlock"), line 8 references `/shm_smoke` (should be `/mlock_smoke`), line 32 defines `FAIL_MARKER = b"___SYSV_SHM_FAIL__"` (should be `___MLOCK_FAIL__`), line 114 defaults `--serial-log` to `/tmp/asterinas-shm-serial.log`, line 153 prints "waiting for shm smoke test result", and lines 165-166 print "=== sysv shm smoke result ===" and "  sysv_shm: PASS".

**Fix.** Replace all remaining `shm`/`SYSV_SHM` references with `mlock`/`MLOCK` equivalents throughout the file.

### `tools/riscv/nixos/mlock/boot_mlock_smoke.py` line 32

Unused constant (minor): `FAIL_MARKER` is defined at line 32 but never referenced anywhere in the file — it is dead code. Additionally, its value (`___SYSV_SHM_FAIL__`) is wrong for this file, a copy-paste artifact.

**Fix.** Either remove the `FAIL_MARKER` constant, or correct its value to `___MLOCK_FAIL__` and use it in the `TimeoutError` handler.

### `tools/riscv/nixos/mlock/boot_mlock_smoke.py` line 49

`dry` (major): The `class Boot` (QEMU serial boot driver with `read_until`, `send`, `close`) is duplicated across ~21 files in `tools/riscv/`. The same ~50-line class with identical `read_until`, `send`, and `close` logic is repeated in every smoke-test driver.

**Fix.** Extract the `Boot` class into a shared module (e.g., `tools/riscv/nixos/boot_common.py`) and import it from each smoke driver. This eliminates ~21 copies of the same class.

### `tools/riscv/nixos/shm/boot_shm_smoke.py` line 32

Unused constant (minor): `FAIL_MARKER` is defined at line 32 but never referenced anywhere in the file — it is dead code.

**Fix.** Either remove the `FAIL_MARKER` constant, or use it in the `TimeoutError` handler to emit a `FAIL_MARKER` check alongside the existing `OK_MARKER` one.

## Correctness

### `kernel/src/device/misc/sound.rs` line 150

> ```diff
> +        let mut data = vec![0u8; len];
> +        current_userspace!().read_bytes(xferi.buf as usize, &mut data)?;
> ```

Unbounded allocation (major): `writei` allocates `vec![0u8; len]` where `len = (xferi.frames as usize) * frame_bytes` with no upper bound beyond checked-mul overflow protection. A malicious user can set `xferi.frames` to a huge value (e.g. 1e9), causing a multi-GB kernel heap allocation that exhausts memory and panics the kernel. Linux bounds `writei` transfers to the PCM buffer size (`runtime->buffer_size`), preventing this.

**Fix.** Cap the transfer size to the negotiated buffer size before allocating:

```rust
let buffer_frames = self.pcm.lock().params()
    .map(|p| p.buffer_frames as usize)
    .unwrap_or(DEV_BUFFER_FRAMES as usize);
let effective_frames = (xferi.frames as usize).min(buffer_frames);
let len = effective_frames.checked_mul(frame_bytes).ok_or_else(|| {
    Error::with_message(Errno::EINVAL, "writei frame size overflows")
})?;
```

### `kernel/src/device/misc/sound.rs` line 220

> ```diff
> +        ostd::warn!("PCM ioctl {:#x}", raw_ioctl.cmd());
> ```

`log-levels` (minor): `ostd::warn!("PCM ioctl {:#x}", raw_ioctl.cmd())` fires on every PCM ioctl call. Per the `log-levels` guideline, `warn!` is for "potentially harmful situations" -- normal ioctl dispatch is not a warning. This floods the log at WARN level during normal operation (every `aplay` run produces ~20 warn lines). The same issue exists in the CTL ioctl handler.

**Fix.** Downgrade to `debug!` for normal ioctl dispatch logging.

### `kernel/src/fs/vfs/notify/fanotify.rs` line 366

> ```diff
> +        {
> +            let marks = self.marks.lock();
> +            for entry in marks.iter() {
> +                if !Weak::ptr_eq(&entry.inode, &inode_weak) { continue; }
> +                ...
> +                return Ok(());
> +            }
> +        }
> +        ...
> +        self.marks.lock().push(entry);
> ```

`atomic-critical-sections` (minor): `add_mark` acquires `self.marks.lock()` to check for an existing mark on the inode, drops the lock, then re-acquires `self.marks.lock()` to push a new entry. Between the two acquisitions another thread could add a mark for the same inode, creating a duplicate entry in the marks vector.

**Fix.** Hold the `marks` lock across the existence check and the push.

### `kernel/src/fs/vfs/notify/fanotify.rs` line 416

> ```diff
> +        let idx = {
> +            let marks = self.marks.lock();
> +            marks
> +                .iter()
> +                .position(|entry| Weak::ptr_eq(&entry.inode, &inode_weak))
> +        };
> +        let Some(idx) = idx else { ... };
> +        let entry = self.marks.lock().remove(idx);
> ```

`atomic-critical-sections` (major): `remove_mark` acquires `self.marks.lock()` to find the index of the entry to remove, drops the lock, then re-acquires `self.marks.lock()` to call `remove(idx)`. Between the two acquisitions the marks vector can be mutated by another thread (e.g. a concurrent `add_mark` or `remove_mark`), making `idx` stale -- the wrong entry may be removed, or the index may be out of bounds.

**Fix.** Hold the `marks` lock across both the `position()` search and the `remove()` call.

### `kernel/src/ipc/shared_memory/shm.rs` line 75

> ```diff
> +    let (vmo, size) = ipc_ns.with_shm_set(shmid, ...)?;
> +    // ... mapping setup ...
> +    let map_addr = options.build()?;
> +    // If this fails, the mapping is orphaned:
> +    ipc_ns.with_shm_set(shmid, ..., |shm_set| {
> +        shm_set.attach(pid);
> +        Ok(())
> +    })?;
> +    ipc_ns.record_shm_attachment(pid, map_addr, shmid);
> ```

Inconsistent state on partial failure (minor): `shm_attach` calls `with_shm_set` to get the VMO and size, sets up the mapping via `options.build()`, then calls `with_shm_set` again to record the attachment. If the second `with_shm_set` call fails (e.g. the shm_set was removed by another thread via `IPC_RMID` while no attachments existed), the mapping is already established in the caller's address space but the attachment is not tracked, leaving an orphaned mapping.

**Fix.** Either defer the mapping setup until after the attachment is recorded, or add rollback logic that unmaps the region if the attachment record fails.

### `kernel/src/syscall/seccomp.rs` line 277

> ```diff
> +        SECCOMP_SET_MODE_STRICT => {
> +            if flags != 0 { ... }
> +            if args != 0 { ... }
> +            ctx.posix_thread.set_seccomp_mode(SECCOMP_MODE_STRICT);
> +            Ok(SyscallReturn::Return(0))
> +        }
> ```

Semantic deviation from Linux (minor): `sys_seccomp(SECCOMP_SET_MODE_STRICT)` does not check whether the thread is already in a seccomp mode. On Linux, re-entering seccomp mode returns `EINVAL`. This implementation silently overwrites the mode, and a thread already in filter mode could be downgraded to strict mode (losing its custom BPF filter).

**Fix.** Check `ctx.posix_thread.seccomp_mode() != SECCOMP_MODE_DISABLED` before entering strict mode and return `EINVAL` if already in a seccomp mode.

### `kernel/src/thread/exception.rs` line 69

> ```diff
> +    let ignored = {
> +        let dispositions = ctx.process.sig_dispositions().lock();
> +        let dispositions = dispositions.lock();
> +        dispositions.get(sig_num).will_ignore(sig_num)
> +    };
> +    if blocked || ignored {
> +        ...
> +        let dispositions = ctx.process.sig_dispositions().lock();
> +        let mut dispositions = dispositions.lock();
> +        dispositions.set_default(sig_num);
> +    }
> ```

`atomic-critical-sections` (minor): `generate_fault_signal` reads the signal disposition under the `sig_dispositions` lock to check `will_ignore`, drops the lock, then re-acquires it to call `set_default`. Another thread can change the disposition from `Ign` to a user handler between the two critical sections via `sigaction`, causing `set_default` to overwrite the newly installed handler.

**Fix.** Hold the `sig_dispositions` lock across both the `will_ignore` check and the `set_default` call.
