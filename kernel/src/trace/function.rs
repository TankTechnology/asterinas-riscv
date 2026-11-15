// SPDX-License-Identifier: MPL-2.0

//! The `mcount` hook that turns `-Z instrument-mcount` into trace records.
//!
//! With that flag the compiler puts `mv a0, ra; call mcount` in the prologue
//! of every function -- including, as the generated assembly shows, in
//! `mcount` itself if `mcount` is ordinary Rust. A plain Rust implementation
//! therefore recurses until the stack ends. A `#[naked]` function is not
//! instrumented (there is no prologue to put the call in), so `mcount` is a
//! few instructions of assembly that:
//!
//! 1. returns at once unless tracing is armed (`MCOUNT_ARMED`, a mirror of the
//!    event's switch, updated whenever the switch is written),
//! 2. takes a re-entry guard with an atomic swap -- the call into the Rust
//!    slow path is instrumented, and the slow path's own prologue comes back
//!    here,
//! 3. calls [`mcount_slow`] with the return address the compiler passed in
//!    `a0`, then releases the guard.
//!
//! The guard is global rather than per CPU: while one CPU is inside the slow
//! path (a few hundred nanoseconds) the others drop their records. Per-CPU
//! would need the CPU id in assembly, which ostd does not expose; this is the
//! documented cost of the feature.

use core::sync::atomic::{AtomicU32, AtomicU64, Ordering};

use ostd::{cpu::PinCurrentCpu, cpu_local, task::disable_preempt};

use super::{event::TraceEvent, events::ftrace::FUNCTION, ring};

/// The assembly-facing half lives in its own module so the crate's
/// `deny(unsafe_code)` can be lifted just here. `#[expect]` is not allowed on
/// a `#[naked]` function, but an `allow` on the enclosing module is.
#[allow(unsafe_code)]
mod hook {
    use core::sync::atomic::{AtomicU32, AtomicU64};

    // The guard must be a real test-and-set. A load-then-store lets two CPUs
    // in at once, and when the first one clears the flag the second recurses
    // into its own (already held, IRQs-off) ring lock: that is a deadlock, not
    // a lost record. `amoswap` needs the `zaamo` target feature, which the
    // function-trace build passes explicitly.

    /// Non-zero while a function trace is armed; read by the assembly stub.
    #[unsafe(no_mangle)]
    pub static MCOUNT_ARMED: AtomicU32 = AtomicU32::new(0);

    /// Non-zero while `mcount_slow` is running; see the module comment.
    #[unsafe(no_mangle)]
    pub static MCOUNT_BUSY: AtomicU32 = AtomicU32::new(0);

    /// Byte offset of the per-CPU guard inside the CPU-local area.
    #[unsafe(no_mangle)]
    pub static MCOUNT_BUSY_OFFSET: AtomicU64 = AtomicU64::new(0);

    /// Called by every instrumented function prologue with the address the
    /// call came from.
    #[unsafe(naked)]
    #[unsafe(no_mangle)]
    pub extern "C" fn mcount() {
        core::arch::naked_asm!(
            // a0 = the caller's return address, put there by the prologue.
            "la   t0, {armed}",
            "lw   t1, 0(t0)",
            "beqz t1, 2f",
            // Per-CPU guard: `gp` holds the CPU-local base, so
            // `gp + offset` is this CPU's flag. A global flag would make every
            // function call on every CPU contend for one cache line, which is
            // slower than the work being traced.
            "la   t0, {offset}",
            "ld   t0, 0(t0)",
            "add  t0, gp, t0",
            "li   t1, 1",
            // The kernel's ISA baseline lacks atomics in LLVM's feature set;
            // enable the one instruction this stub needs, locally.
            ".option push",
            ".option arch, +zaamo",
            "amoswap.w.aq t2, t1, (t0)",
            ".option pop",
            "bnez t2, 2f",
            // Save `ra`: the naked function has no frame of its own.
            "addi sp, sp, -16",
            "sd   ra, 8(sp)",
            // Report the *callee*: `ra` here is the address right after the
            // prologue's `call mcount`, which lies inside the function being
            // entered. The compiler's `a0` is where the callee was called
            // from, i.e. the caller.
            "mv   a0, ra",
            "call {slow}",
            "ld   ra, 8(sp)",
            "addi sp, sp, 16",
            "la   t0, {offset}",
            "ld   t0, 0(t0)",
            "add  t0, gp, t0",
            "sw   zero, 0(t0)",
            "2:",
            "ret",
            armed = sym MCOUNT_ARMED,
            offset = sym MCOUNT_BUSY_OFFSET,
            slow = sym super::mcount_slow,
        )
    }
}

pub use hook::{MCOUNT_ARMED, MCOUNT_BUSY_OFFSET};

cpu_local! {
    /// Non-zero while this CPU is inside `mcount_slow`.
    static MCOUNT_BUSY: AtomicU32 = AtomicU32::new(0);
}

/// Reads the CPU-local base register. RISC-V keeps it in `gp`; ostd's own
/// CPU-local accessors use the same register.
#[expect(unsafe_code)]
fn cpu_local_base() -> usize {
    let base: usize;
    // SAFETY: reading `gp` has no side effects; ostd keeps it pointing at the
    // CPU-local area whenever kernel code runs.
    unsafe {
        core::arch::asm!("mv {}, gp", out(reg) base, options(nomem, nostack, preserves_flags));
    }
    base
}

/// Records where this CPU's guard lives, for the assembly stub.
pub fn init() {
    let guard = disable_preempt();
    let cell: &AtomicU32 = MCOUNT_BUSY.get_on_cpu(guard.current_cpu());
    let offset = cell as *const AtomicU32 as u64 - cpu_local_base() as u64;
    MCOUNT_BUSY_OFFSET.store(offset, Ordering::Relaxed);
}

/// Mirrors the event's switch for the assembly stub.
pub fn sync_armed() {
    let armed = if FUNCTION.enabled() { 1 } else { 0 };
    MCOUNT_ARMED.store(armed, Ordering::Relaxed);
}

/// The Rust half of `mcount`: records one entry into the function whose
/// prologue called the stub.
#[unsafe(no_mangle)]
#[expect(unsafe_code)]
pub extern "C" fn mcount_slow(caller_ra: u64) {
    let (pid, tid) = ostd::task::Task::current()
        .map(|task| crate::thread::task_ids(&task))
        .unwrap_or((0, 0));
    // Jiffies, not the monotonic clock: this can run inside the clocksource's
    // own update (the timer interrupt calls functions too), and the clock's
    // read lock is held there.
    let elapsed = ostd::timer::Jiffies::elapsed().as_duration();
    if crate::bpf::attach::is_attached(FUNCTION.id()) {
        crate::bpf::attach::run_at(
            FUNCTION.id(),
            pid,
            tid,
            [caller_ra, 0, 0],
            elapsed.as_nanos() as u64,
        );
    }
    ring::push_at(FUNCTION.id(), pid, tid, &caller_ra.to_le_bytes(), elapsed);
}

/// The function trace's own switch, kept next to the stub that reads it.
pub fn event() -> &'static TraceEvent {
    &FUNCTION
}
