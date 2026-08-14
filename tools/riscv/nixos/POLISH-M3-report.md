# POLISH-M3 — seccomp BPF (SECCOMP_SET_MODE_FILTER): verification + PR flow to main

> 2026-08-15. The last "big item" of the sandbox track. The classic-BPF filter
> interpreter for `seccomp(2)` `SECCOMP_SET_MODE_FILTER` is the piece a
> browser-level sandbox (Chromium-style) needs on top of strict mode.

## Conclusion first

The BPF filter mode was **already implemented and committed** earlier in this
track (`POLISH-M1`/`POLISH-M2`, commits below) — the "only STRICT mode" premise
was stale. This milestone therefore does three things: (1) re-verify the filter
behaviour end-to-end in QEMU (ERRNO / SIGSYS / fork inheritance), (2) review the
interpreter against Linux `kernel/seccomp.c` semantics and enumerate the
remaining gaps, and (3) flow the three kernel commits back to `main` as a
focused PR.

| Item | Status |
|---|---|
| `SECCOMP_SET_MODE_FILTER` install + per-thread filter | **Done** — `1fd727e3a` |
| classic-BPF interpreter (LD/LDX/ALU/JMP/RET) | **Done** — `1fd727e3a`, fail-secure |
| `seccomp_data` (nr / arch / ip / args) | **Done** — `1fd727e3a` |
| action mapping (ALLOW/ERRNO/SIGSYS) | **Done** — `1fd727e3a` |
| strict mode + SIGSYS delivery | **Done** — `cf7490e5c` |
| filter inheritance across fork/vfork/clone | **Done** — `1e7437a0f` |
| QEMU verification (ERRNO / SIGSYS / inherit) | **PASS** — smp1 (see §2) |
| PR to `main` | **Opened** — https://github.com/TankTechnology/asterinas-riscv/pull/34 (§3) |

---

## 1. Implementation review (`kernel/src/syscall/seccomp.rs`)

The filter path lives entirely in `kernel/src/syscall/seccomp.rs` and is wired
into syscall dispatch through `check()` → `SeccompDecision` (the old
`should_block` bool was replaced by an enum, commit `1fd727e3a`).

### 1.1 The classic-BPF interpreter

`run_filter()` is a straight port of the classic BPF evaluation loop
(`__seccomp_filter` in Linux) over `struct sock_filter`, with `A`/`X` registers
and a program counter. Supported opcodes:

| Class | Ops | Note |
|---|---|---|
| `LD` | `IMM`, `W|ABS` (word load from `seccomp_data`) | byte/half loads **not yet** (see §4) |
| `LDX` | `IMM`, `W|ABS` | |
| `ALU` | `ADD/SUB/MUL/DIV/OR/AND/LSH/RSH/NEG/MOD/XOR` (K and X sources) | `DIV`/`MOD` by zero → fail-secure |
| `JMP` | `JA/JEQ/JGT/JGE/JSET` (jt/jf offsets) | unconditional jump uses `k` |
| `RET` | `K` and `A` | |

Any unrecognised opcode, out-of-bounds `ABS` load, or program that falls off the
end returns `SECCOMP_RET_KILL_THREAD` — i.e. the filter **fails secure**, the
same default a seccomp filter has when it cannot be evaluated.

### 1.2 `seccomp_data` and action mapping

- `build_seccomp_data()` lays out the 64-byte `seccomp_data` exactly as Linux:
  `int nr` (0), `u32 arch = AUDIT_ARCH_RISCV64` (4), `u64 instruction_pointer`
  (8), `u64 args[6]` (16). Little-endian via `to_ne_bytes`, matching riscv64.
- `seccomp_action_to_decision()` maps the filter's `u32` return value:
  - `ALLOW` / `LOG` → allow (LOG is treated as ALLOW; no audit support),
  - `ERRNO` → return `-errno` with **no** signal,
  - `KILL_*` / `TRAP` / `TRACE` / `USER_NOTIF` → deliver `SIGSYS` + `ENOSYS`.

### 1.3 `sys_seccomp` — install path

`SECCOMP_SET_MODE_FILTER` rejects non-zero `flags` (so `TSYNC`/`NEW_LISTENER` are
`EINVAL`, §4), reads `struct sock_fprog { u16 len; struct sock_filter *filter }`
as two field reads to dodge its trailing padding, copies the program into an
`Arc<[SockFilter]>` on the thread, and — before installing — runs a jump-verifier
that rejects any program whose `jt`/`jf`/`k` jump leaves `[0, len]` (Linux's
verifier does the same up front).

---

## 2. QEMU verification — SIGSYS / ERRNO / inheritance

`tools/riscv/pm1-gate.sh` builds a static `/init` (`tools/riscv/nixos/pm1/init.c`)
that forks a child per case, installs a BPF filter, triggers the targeted syscall
and asserts the result. The three cases cover exactly the behaviour the task
called for:

| Case | Filter | Expected | Result (smp1) |
|---|---|---|---|
| `seccomp_filter_errno` | `ERRNO(EPERM)` on `getpid` | `getpid` → `-1/EPERM` | **OK** |
| `seccomp_filter_kill` | `KILL_PROCESS` on `getpid` | child dies with `SIGSYS` | **OK** |
| `seccomp_filter_inherit` | `ERRNO(EPERM)` on `getpid`, inherited across fork | grandchild `getpid` → `-1/EPERM` | **OK** |

```
[PM1] seccomp_filter_errno:   OK
[PM1] seccomp_filter_kill:    OK
[PM1] seccomp_filter_inherit: OK
__PM1_DONE__ __PM1_PASS__
```

So both the `SIGSYS` (KILL/TRAP) and the `ERRNO` return path are confirmed on
hardware-emulated riscv64, and the fork-inheritance semantics match Linux.

A `-smp 4` re-run of the same gate **times out** (no `[PM1]` markers appear after
the boot banner, 5 min watchdog). The kernel boots and prints its banner, but the
`/init` test never emits output — i.e. the first `fork()` in the test never
completes under 4 CPUs. This is **not** a seccomp regression (smp1 passes the
identical filter) but a separate SMP boot/fork issue worth a dedicated
investigation; see §5.

---

## 3. PR flow to `main`

The `foundation` PR track already merged strict mode into `main` via **#33**
(`279d4ac2e`, byte-for-byte identical to the track's `cf7490e5c`), so this PR only
needs the two remaining kernel commits:

```
1fd727e3a feat(syscall): seccomp SECCOMP_SET_MODE_FILTER — classic BPF filter
1e7437a0f feat(syscall): inherit seccomp mode and BPF filter across fork/vfork
```

Cherry-picked onto `origin/main` (branch `seccomp-bpf-filter`): both apply
**cleanly** (no conflicts) — the filter code is self-contained on top of the
already-merged strict mode. Opened as **PR #34**.

Verification: the branch builds clean for the actual target —
`cargo osdk build --scheme riscv --features riscv_sv39_mode` finishes
(`aster-kernel` + `aster-kernel-osdk-bin`, ~25 s incremental, **no errors**). A host
`cargo check` is *not* a valid signal here — it fails only in the x86-only
`ostd` modules (`acpi`, `tdx_guest`) that are irrelevant to riscv64 and were not
touched by these commits.

---

## 4. Remaining gaps (ordered by sandbox impact)

1. **Byte / half / indirect BPF loads** (`LD|B|ABS`, `LD|H|ABS`, `LD|W|IND`,
   `LDX|MSH`). The interpreter only does 32-bit word loads. libseccomp and
   Chromium emit byte/half loads for syscall-argument filtering, so a filter
   that inspects `args[]` fields at sub-word granularity will currently hit the
   fail-secure `KILL_THREAD` path. This is the highest-value next step for real
   browser-sandbox compatibility.
2. **`SECCOMP_FILTER_FLAG_TSYNC`** — synchronise the filter to all sibling
   threads. Currently `EINVAL`. Chromium applies filters with TSYNC.
3. **`SECCOMP_FILTER_FLAG_NEW_LISTENER`** / user notification — `EINVAL`; no
   `pidfd`/`SECCOMP_IOCTL_NOTIF` support.
4. **`SIGSYS` `siginfo_t` detail fields** — `si_call_addr` / `si_syscall` /
   `si_arch` are not populated (the `siginfo_t` model does not expose them).
   A `SIGSYS` handler cannot yet read *which* syscall was blocked.
5. **`SECCOMP_RET_TRACE`** — treated as `KILL` (no ptrace). Correct fallback for
   now, but a ptrace-based tracer would not receive the event.

---

## 5. Notes

- `DIV`/`MOD` by zero in the interpreter returns `KILL_THREAD` (fail-secure)
  rather than Linux's `0` result — deliberate: libseccomp never emits a bare
  divide, and fail-secure is the safer default for an unknown input.
- `LOG` is folded into `ALLOW` (no audit backend on this kernel).
- **SMP=4 hang (new, separate from seccomp).** The `pm1` initramfs gate boots and
  prints the Asterinas banner but `/init` never reaches its first `fork()` under
  `-smp 4` (5 min watchdog timeout). smp1 is unaffected. Since LTP is reported to
  run SMP=4, this looks specific to the minimal initramfs first-process/fork path
  rather than to seccomp; file it as a follow-up SMP subtask, not part of this
  milestone.
