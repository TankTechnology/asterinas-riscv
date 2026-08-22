# M4 Report: clone/CLONE_SETTLS thread bug — root cause + kernel fix

> 2026-08-13. Follow-up to `M3-report.md` "Blocking gap". Goal was to fix the
> `clone`/`CLONE_SETTLS` thread bug that made `nix --version` print its banner
> but never exit, blocking `nix eval`.
>
> Conclusion up front: **the M3 hypothesis was wrong — `tp` is set correctly.**
> The real kernel defect is that a synchronous fault signal (`SIGSEGV`) was
> **never delivered when the faulting thread had it blocked**, so the faulting
> instruction re-executed forever. That is now fixed (`thread/exception.rs`).
> After the fix the thread is killed by `SIGSEGV` instead of looping. The
> *remaining* blocker for `nix eval` is a deeper one — **general-dynamic TLS
> (the DTV) is NULL in a freshly cloned thread** — documented below.

## TL;DR

| Finding | Status |
|---|---|
| `CLONE_SETTLS` sets the child `tp` register | ✅ already correct (`tp == tls`) |
| Unmapped user page fault → `SIGSEGV` | ✅ fixed (was dropped when blocked) |
| `nix --version` exits (no infinite loop) | ✅ fixed |
| `nix eval --expr '1 + 1'` → `2` | ❌ still blocked (DTV NULL in new thread) |

## Root-cause analysis

### 1. `CLONE_SETTLS` is fine (M3 report was wrong)

Instrumentation in `clone_tls_pointer` (`process/clone.rs`) showed:

```
CLONE_SETTLS: tls=0x3ffeffee40, tp_before=0x3ffd3830e8
```

and at the fault site the child thread's `tp` register was:

```
user fault: exc=StorePageFault(8), pc=0x3ffefb721c, tp=0x3ffeffee40
```

`tp == tls`, i.e. the kernel correctly restores the RISC-V `tp` register for
the cloned child. The M3 hypothesis ("`tp` is 0, so `tp[1]` resolves to
`NULL+8`") was incorrect — the fault at `0x8` is **not** a `tp`-relative access.
`set_tls_pointer` → `set_tp` in `ostd/src/arch/riscv/cpu/context.rs` works, and
the trap frame in `ostd/src/arch/riscv/trap/trap.S` saves/restores `x4` (`tp`)
correctly.

### 2. The real bug: synchronous fault signals are dropped when blocked

`kernel/src/thread/exception.rs::handle_exception` does turn an unmapped user
page fault into a `SIGSEGV` (`generate_fault_signal` → `to_fault_signal` →
`enqueue_signal`). The problem is the *delivery* side: `dequeue` in
`process/signal/sig_queues.rs` skips any signal that is blocked in the thread's
mask, and nothing unblocks it. musl's thread startup blocks all signals, so the
faulting child's `SIGSEGV` was never dequeued — the faulting store re-executed
in an infinite loop, exactly the M3 symptom (`page fault handler failed` logged
every ~5 ms forever, no `SIGSEGV` delivery, no `exit_group`).

Linux avoids this with `force_sig_fault_to_task`: a *synchronous* fault signal
that is blocked or ignored is unblocked and its disposition is reset to the
default action.

### The fix (`kernel/src/thread/exception.rs`)

```rust
fn generate_fault_signal(exception: CpuException, ctx: &Context, user_ctx: &UserContext) {
    let Some(signal) = exception.to_fault_signal(user_ctx) else {
        panic!("`{:?}` cannot be handled via signals", exception);
    };
    let sig_num = signal.num();

    // Synchronous fault signals must be delivered even if blocked/ignored,
    // otherwise the faulting instruction re-executes forever. Mirror Linux's
    // force_sig_fault_to_task: unblock and reset to the default action.
    let blocked = ctx.posix_thread.sig_mask().contains(sig_num);
    let ignored = {
        let dispositions = ctx.process.sig_dispositions().lock();
        let dispositions = dispositions.lock();
        dispositions.get(sig_num).will_ignore(sig_num)
    };
    if blocked || ignored {
        if blocked {
            let mut mask = ctx.posix_thread.sig_mask();
            mask -= sig_num;
            ctx.set_sig_mask(mask);
        }
        let dispositions = ctx.process.sig_dispositions().lock();
        let mut dispositions = dispositions.lock();
        dispositions.set_default(sig_num);
    }

    ctx.posix_thread.enqueue_signal(Box::new(signal));
}
```

## Verification

After the fix, the M3 smoke log shows the previously-looping thread is now
killed:

```
WARN: page fault handler failed: ... address: 0x8 ...
WARN: PID 2: terminating on signal SIGSEGV
WARN: PID 2: terminating on signal SIGKILL
```

`nix --version` no longer hangs; each nix process is torn down by `SIGSEGV`.
The minimal repro (`tools/riscv/nixos/m4/tls_repro.c`) passes both checks:

```
__M4_TLS_OK__ child tp=0x3ffeffb000, tp[1] write+readback ok
__M4_SEGV_OK__ child killed by SIGSEGV
```

## Remaining gap: general-dynamic TLS (DTV) is NULL in a new thread

With the signal loop fixed, nix still does not reach `nix eval`: its Boehm-GC
thread faults during startup. The fault is now the *trigger* rather than a
delivery problem, and it is TLS-related, not clone-related.

`tools/riscv/nixos/m4/{libtls.c,tls_shared.c}` isolate it. A `__thread` variable
in a **shared object** (general-dynamic TLS, accessed through the DTV via
`__tls_get_addr`) is fine in the main thread but faults in a freshly cloned
thread:

```
main: *p=99 (addr 0x3ffeffeda8)          # main-thread DTV is set up
... page fault ... address: 0x8, required_perms: READ   # child DTV is NULL
```

The child's `self->dtv` (pthread struct offset 8) is NULL, so
`__tls_get_addr` reads `dtv[module_id]` at address `module_id * 8` → `0x8`.
Local-exec TLS in the main executable works for new threads (verified with a
`__thread` variable in the main binary, value `42` copied correctly); only the
shared-library / DTV path is broken. The DTV for a new thread is populated by
musl's `__copy_tls` in the parent before `clone`, so the gap is likely in how
Asterinas presents ELF/auxv information that musl's TLS setup consumes — to be
investigated next.

## Reproducing

```bash
# Kernel (needs a dummy initramfs for the OSDK manifest, the riscv sv39 feature,
# the prebuilt riscv64 vDSO, and rust-objcopy from llvm-tools on PATH):
#   touch test/initramfs/build/initramfs.cpio.gz (one-time)
export PATH="$HOME/.rustup/toolchains/nightly-2026-07-21-x86_64-unknown-linux-gnu/lib/rustlib/x86_64-unknown-linux-gnu/bin:$PATH"
cd kernel && OSDK_TARGET_ARCH=riscv64 \
  VDSO_LIBRARY_DIR="$HOME/.local/share/linux_vdso" \
  cargo osdk build --scheme riscv --features riscv_sv39_mode

# Minimal clone/CLONE_SETTLS + SIGSEGV repro:
tools/riscv/nixos/m4/build_m4.sh
python3 tools/riscv/nixos/m4/boot_m4_smoke.py

# M3 smoke (nix --version):
#   repack boot.ext4 with the new Image + target/nixos/m3/m3-initramfs.cpio.gz
python3 tools/riscv/nixos/m3/boot_m3_smoke.py
```

## Files changed

- `kernel/src/thread/exception.rs` — force-deliver synchronous fault signals.
- `tools/riscv/nixos/m4/` — minimal repro (`tls_repro.c`, `build_m4.sh`,
  `boot_m4_smoke.py`) + shared-TLS repro (`libtls.c`, `tls_shared.c`).
