# M5 Report: general-dynamic TLS (DTV) in cloned threads — root cause + fix

> 2026-08-13. Follow-up to `M4-report.md` "Remaining gap: general-dynamic TLS
> (DTV) is NULL in a new thread". Goal: make the Boehm-GC thread in `nix` start
> cleanly so that `nix eval` becomes reachable.
>
> Conclusion up front: **the gap was not in the ELF loader or auxv.** The
> RISC-V `clone` syscall uses Linux's `CONFIG_CLONE_BACKWARDS` argument order —
> `clone(flags, stack, ptid, tls, ctid)` — i.e. the `tls` and `ctid` arguments
> are swapped relative to x86_64. Asterinas's `sys_clone` read them in the
> x86_64 order, so on RISC-V the kernel set the child's `tp` register to the
> `ctid` value instead of the TLS pointer. With the swap fixed, `nix eval` works:
> `nix eval --expr '1 + 1'` prints `2`.

## TL;DR

| Finding | Status |
|---|---|
| `nix --version` prints banner | ✅ (unchanged) |
| `nix --version` exits cleanly | ✅ (was a hang; now fixed by M4 + M5) |
| New thread's `tp` == CLONE_SETTLS TLS pointer | ✅ fixed (was `ctid`) |
| Shared-library general-dynamic TLS in child thread | ✅ fixed (DTV no longer NULL) |
| `nix eval --expr '1 + 1'` → `2` | ✅ fixed |

## Root cause

### The M4 hypothesis (ELF/auxv) was wrong

M4 suspected the kernel failed to pass `AT_PHDR`/`AT_PHENT`/`AT_PHNUM` or the
`PT_TLS` program headers to musl. That is **not** the case: the ELF loader
(`kernel/src/process/program_loader/elf/load_elf.rs::init_aux_vec`) and the
auxv build path are architecture-independent and correctly populate the auxv,
and the main thread's DTV is built correctly (which is why shared `__thread`
variables work on the main thread).

### The real bug: `clone` argument order on RISC-V

RISC-V selects `CONFIG_CLONE_BACKWARDS` in Linux, so the raw `clone` syscall
signature is (see `arch/riscv/kernel/process.c`):

```c
SYSCALL_DEFINE5(clone, unsigned long clone_flags, unsigned long newsp,
                int __user *parent_tidptr,
                unsigned long tls,          // a3
                int __user *child_tidptr)   // a4
```

On x86_64 the last two arguments are the other way around
(`child_tidptr` then `tls`). Asterinas's `sys_clone` declared its parameters in
the x86_64 order and the generic syscall table dispatched
`sys_clone(args[..5])`, so on RISC-V the kernel read `child_tidptr` from `a3`
(= `tls`) and `tls` from `a4` (= `child_tidptr`).

musl passes `tls = TP_ADJ(new)` (the correct thread pointer) in `a3` and
`ctid` in `a4`, so the swapped read made the kernel set the child's `tp`
register to `ctid` — for musl 1.2.6 that is `&__thread_list_lock`, a `.bss`
address in libc. The child then computed its DTV pointer as `*(tp - 8)` (musl's
riscv64 `__tls_get_addr` does `ld a5, -8(tp)`), read `0`, and faulted reading
`dtv[module_id]` at address `module_id * 8` (0x8/0x10) — the exact symptom M4
reported. Kernel instrumentation confirmed the swap:

```
# before fix (wrong):  tls read from a4 == &__thread_list_lock
M5 sys_clone: flags=0x7d0f00 new_sp=... ptid=0x...28 ctid=0x...bd0 tls=0x3ffeffee70
# expected:            tls == TP_ADJ(new) == 0x...bd0 (== the `ctid` slot above)
```

### The fix (`kernel/src/syscall/clone.rs`)

Swap `child_tidptr` and `tls` on the CLONE_BACKWARDS architectures before
building the architecture-independent `CloneArgs`:

```rust
#[cfg(any(target_arch = "riscv64", target_arch = "loongarch64"))]
let (child_tidptr, tls) = (tls as Vaddr, child_tidptr as u64);
```

LoongArch also selects `CONFIG_CLONE_BACKWARDS`, so it is gated the same way
(untested here; only RISC-V was exercised).

### Also fixed: the M4 repro used the wrong ABI

`tools/riscv/nixos/m4/tls_repro.c`'s raw `clone` wrapper placed `tls` in `a4`
and `ctid` in `a3` (the x86_64 layout). It happened to "pass" against the old,
also-wrong kernel, but would have regressed after the fix. It now places `tls`
in `a3` and `ctid` in `a4` to match the RISC-V ABI.

## Verification

### M4 smoke (`tools/riscv/nixos/m4/boot_m4_smoke.py`)

The repro was extended to also run the shared-library TLS case
(`libtls.c` + `tls_shared.c`, dynamic musl + `pthread_create`) after the static
clone/SIGSEGV checks:

```
=== M4 smoke results ===
  clone CLONE_SETTLS -> tp: OK
  user fault -> SIGSEGV: OK
  shared-lib general-dynamic TLS: OK
```

### M3 smoke (`tools/riscv/nixos/m3/boot_m3_smoke.py`)

`nix --version` now exits cleanly (its Boehm-GC thread no longer faults) and
both eval commands produce their output:

```
=== M3 smoke results ===
  nix --version: OK
  nix --version exits cleanly: OK
  nix eval -> 2: OK
  nix eval --raw -> hello: OK
```

### A second, non-kernel blocker surfaced once threads worked

After the TLS fix, `nix eval` still failed with:

```
error: experimental Nix feature 'nix-command' is disabled
```

This is a Nix configuration issue, not a kernel gap. `build_m3.sh` now writes
`experimental-features = nix-command flakes` into `/etc/nix/nix.conf`, and
`init_m3.c`'s smoke script captures each eval result behind a fixed marker
(`eval_result=[...]` / `hello_result=[...]`) so the QEMU driver can assert the
output unambiguously despite `loglevel=info` syscall logging interleaving with
user output.

## Remaining gaps

| Gap | Impact |
|---|---|
| `membarrier` (283) ENOSYS | called by libstdc++ during thread setup; harmless |
| `riscv_hwprobe` (258) ENOSYS | startup probe; harmless |
| `rseq` (293) ENOSYS | startup probe; harmless |
| kernel panics when init exits (`kernel/src/init.rs:108`) | expected teardown; not a blocker for the smoke tests |

No kernel gap blocks `nix eval` for the expressions tested. Larger nixpkgs
flakes still need a writable `/nix/store`, `AF_UNIX` for the daemon, and network
fetch of the flake (documented in `M3-report.md` "Next steps").

## Files changed

- `kernel/src/syscall/clone.rs` — swap `child_tidptr`/`tls` on CLONE_BACKWARDS.
- `tools/riscv/nixos/m4/tls_repro.c` — correct RISC-V clone ABI; chain into the
  shared-TLS repro.
- `tools/riscv/nixos/m4/build_m4.sh` — build the dynamic musl shared-TLS repro.
- `tools/riscv/nixos/m4/boot_m4_smoke.py` — assert the shared-TLS marker.
- `tools/riscv/nixos/m3/build_m3.sh` — enable `nix-command` experimental feature.
- `tools/riscv/nixos/m3/init_m3.c` — capture eval results behind markers.
- `tools/riscv/nixos/m3/boot_m3_smoke.py` — assert version-exit + eval outputs.
