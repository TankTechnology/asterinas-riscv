// SPDX-License-Identifier: MPL-2.0

//! Lock-free telemetry.
//!
//! Every existing observation channel holds the console lock with local IRQs
//! disabled while it writes, which makes the observer part of the system it
//! measures: a 400-byte line is a ~35 ms IRQ-off window on the hart that
//! prints it. This channel writes single bytes straight to the firmware
//! console instead -- no console lock, no local IRQ disable -- at a chosen
//! rate, so a board that dies leaves the last few tens of milliseconds of
//! state behind instead of the last second.
//!
//! Lines are prefixed with `T` and may interleave with normal log lines.
//! `asterinas.telemetry_hz=<n>` turns it on (1..=50, default off); the bytes
//! it writes count as console traffic, so a run that enables it is not
//! directly comparable with one that does not.

use alloc::string::String;
use core::sync::atomic::{AtomicU32, Ordering};

use aster_cmdline::define_kv_param;

static REQUESTED_HZ: AtomicU32 = AtomicU32::new(0);
define_kv_param!("asterinas.telemetry_hz", REQUESTED_HZ);

/// Starts the telemetry thread when `asterinas.telemetry_hz` asks for one.
pub(crate) fn spawn_if_requested() {
    let hz = REQUESTED_HZ.load(Ordering::Relaxed);
    if hz == 0 {
        return;
    }
    let hz = u64::from(hz.clamp(1, 50));
    ostd::early_println!("ASTERINAS_TELEMETRY status=armed hz={}", hz);
    crate::thread::kernel_thread::ThreadOptions::new(move || worker(hz)).spawn();
}

fn worker(hz: u64) {
    let frequency = ostd::arch::tsc_freq().max(1);
    let interval = (frequency / hz).max(1);
    let yield_threshold = (frequency / 100_000).max(1);
    let mut next = ostd::arch::read_tsc();
    loop {
        next = next.saturating_add(interval);
        // Pace without monopolizing the hart: a busy thread here would change
        // the system's scheduling just like the stress worker did.
        loop {
            let now = ostd::arch::read_tsc();
            if now >= next {
                break;
            }
            if next - now > yield_threshold {
                crate::thread::Thread::yield_now();
            } else {
                core::hint::spin_loop();
            }
        }
        emit();
    }
}

fn emit() {
    let (dispatches, with_remote, _) = ostd::mm::tlb::dispatch_counters();
    let sbi_calls = ostd::smp::sbi_ipi_call_count();
    let uptime_ms = ostd::timer::Jiffies::elapsed().as_duration().as_millis();

    let mut line = String::from("T");
    line.push_str(&alloc::format!(
        "{} dx={} dxr={} sbi={}",
        uptime_ms,
        dispatches,
        with_remote,
        sbi_calls
    ));
    for cpu in 0..ostd::cpu::num_cpus() {
        let (name, age_us) = ostd::smp::activity_of(cpu);
        line.push_str(&alloc::format!(" {}:{}:{}", cpu, name, age_us / 1000));
        let (_, _, pc) = ostd::smp::trap_stats(cpu);
        line.push_str(&alloc::format!("@{:#x}", pc));
        let trap_age = ostd::smp::trap_age_us(cpu);
        line.push_str(&alloc::format!("+{}", trap_age / 1000));
    }
    line.push('\n');

    // One byte at a time, straight to the firmware console: no lock to hold,
    // no interrupts to disable. A slow UART paces the thread, and that is
    // bounded by the line length.
    //
    // The firmware console exists only on RISC-V; other architectures build
    // this crate too and must not fail on the reference.
    #[cfg(target_arch = "riscv64")]
    for byte in line.as_bytes() {
        ostd::console_write_byte(*byte);
    }
    #[cfg(not(target_arch = "riscv64"))]
    let _ = line;
}
