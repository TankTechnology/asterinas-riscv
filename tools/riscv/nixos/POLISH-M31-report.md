# POLISH-M31 — nice(2) via CAP_SYS_NICE in setpriority: nice01 FAIL → PASS, nice02/03 hold

Date: 2026-08-21
Branch: `track/nixos`
Change: `kernel/src/syscall/set_priority.rs` (working tree, uncommitted)
Status: **Complete — `nice01` passes; `nice02`/`nice03` regression-checked PASS.**

## 1. Root-cause analysis (correcting the M27/M28 note)

M27 classified `nice01` as `nice(-1) EPERM` with "No CAP_SYS_NICE check"; the M28
session note added "the `sys_nice` syscall doesn't exist in the syscall table at all".

The second half is a red herring: **no 64-bit Linux architecture has a dedicated
`nice(2)` syscall.** `__NR_nice` exists only on 32-bit (i386 = 34). On both x86-64 and
riscv64 (asm-generic), libc emulates `nice()` on top of `getpriority` + `setpriority`.
Verified by disassembling the musl 1.2.6 `nice()` in the cross sysroot used for the
LTP binaries:

1. if `inc ∈ [-39, 39]`: `prio = getpriority(PRIO_PROCESS, 0) + inc`, else `prio = inc`
2. clamp `prio` to `[-20, 19]`
3. `setpriority(PRIO_PROCESS, 0, prio)`
4. on failure: if `errno == EACCES`, rewrite `errno = EPERM` (per POSIX) and return -1;
   on success return the computed prio.

So the observed `EPERM` was musl rewriting the kernel's `EACCES`, and the real kernel
gap was in `sys_set_priority`: it returned `EACCES` whenever the target nice value was
below the `RLIMIT_NICE`-derived limit (default 0), with **no `CAP_SYS_NICE` override** —
so even root could not lower its nice value.

## 2. Fix

`kernel/src/syscall/set_priority.rs` now mirrors Linux's `setpriority(2)` EACCES rule:

```rust
let caller_caps = ctx.posix_thread.credentials().effective_capset();
...
let cur_nice = process.nice().load(Ordering::Relaxed);
if new_nice < cur_nice && new_nice < limit && !caller_caps.contains(CapSet::SYS_NICE) {
    return_errno!(Errno::EACCES);
}
```

i.e. EACCES only when the caller (a) *lowers* the nice value below the target's current
value, (b) below the RLIMIT_NICE-derived limit, and (c) lacks `CAP_SYS_NICE` in its
effective set. Root in the initramfs holds the full effective capset, so
`setpriority(0, 0, -1)` now succeeds.

`sys_get_priority` needed no change: it already returns `20 - nice` at the syscall
boundary (the raw-syscall encoding), which musl's `getpriority` wrapper translates back.

## 3. Verification

54-tag SMP=1 subset (kernel rebuilt with this change together with the M29 loop fixes):

```
[PASS] nice01
```

plus a dedicated 3-tag re-run:

```
[summary] total=3 pass=3 fail=0 conf=0 crash=0 timeout=0   (nice01 nice02 nice03)
```

`nice01` (root, `nice_inc[] = {-1, -12, -50}`) exercises exactly the privileged path:
`nice(-50)` clamps to `-20`, far below the RLIMIT_NICE limit, and only passes with the
`CAP_SYS_NICE` override. `nice02` (unprivileged raise-lower) and `nice03` confirm the
unprivileged path still works.

## 4. Known residual gaps (not needed by nice01–03)

- **No cross-user EPERM check**: Linux returns EPERM when modifying a process owned by
  a different (r/e)uid without `CAP_SYS_NICE`; `get_processes()` currently applies the
  change to any resolvable target.
- **Simplified RLIMIT_NICE formula**: the limit is taken as `rlim_cur` directly instead
  of Linux's `20 - rlim_cur` ceiling. Identical at the default (`rlim_cur = 0`).
- **Scheduler state not synced**: `setpriority` updates only the per-process nice value;
  the per-thread `SchedPolicy::Fair` state is a pre-existing FIXME.

## 5. Conclusion

| Metric | Value |
|---|---|
| `nice01` | FAIL (EPERM) → **PASS** |
| `nice02`, `nice03` | PASS (no regression) |
| Files changed | 1 (`kernel/src/syscall/set_priority.rs`) |
| New syscall table entries | 0 (none needed — no 64-bit arch has `__NR_nice`) |
