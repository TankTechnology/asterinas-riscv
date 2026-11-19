// SPDX-License-Identifier: MPL-2.0

//! Keep the EIC7700 watchdog alive after U-Boot hands control to Asterinas.
//!
//! U-Boot arms this watchdog before a physical boot so that a kernel which
//! never reaches its timer path still recovers. The DesignWare block cannot be
//! stopped after it is enabled, so a successful boot must take over the
//! heartbeat. This module is opt-in and fail-closed: it only maps the fixed
//! watchdog window after the command line request and device-tree compatible
//! have both been checked.

use alloc::string::String;
use core::{
    sync::atomic::{AtomicBool, AtomicU32, Ordering},
    time::Duration,
};

use aster_cmdline::define_kv_param;
use ostd::{arch::boot::DEVICE_TREE, io::IoMem, mm::io::VmIoOnce, timer};
use spin::Once;

const WATCHDOG_BASE: usize = 0x5080_0000;
const WATCHDOG_LIMIT: usize = 0x100;
/// EIC7700 syscfg register that holds the watchdog block in reset.
const EIC7700_WDT_RESET_CONTROL: usize = 0x5182_8444;
/// Value that releases the watchdog reset; zero holds it asserted, which is
/// why `COMP_TYPE` reads as zero on a boot U-Boot did not arm.
const EIC7700_WDT_RESET_RELEASE: u32 = 1;
const CONTROL_OFFSET: usize = 0x00;
/// Timeout-range register; only the low four bits (TOP) are implemented.
const TOP_OFFSET: usize = 0x04;
const COUNTER_RESTART_OFFSET: usize = 0x0c;
const COMPONENT_TYPE_OFFSET: usize = 0xfc;
const CONTROL_ENABLE: u32 = 1;
/// Enable plus "interrupt then reset", the mode U-Boot arms.
const CONTROL_ARM_WITH_RESET: u32 = 0x1f;
/// Largest TOP the counter supports; about 10.7 s at the block's 200 MHz.
const TOP_MAX: u32 = 0xf;
const COUNTER_RESTART_VALUE: u32 = 0x76;
/// The `COMP_TYPE` signature of the DesignWare watchdog.
///
/// This is a fixed peripheral id, not a version: the version lives in
/// `COMP_VERSION` at `0xf8`. `0x4457_0140` is a different DesignWare block
/// (I2C), so the comparison below stays exact.
const COMPONENT_TYPE: u32 = 0x4457_0120;
const KICK_PERIOD: Duration = Duration::from_secs(1);

/// How often the heartbeat says it is still alive (once every five kicks).
const HEARTBEAT_REPORT_EVERY: u32 = 1;

static REQUESTED: Once<String> = Once::new();
define_kv_param!("asterinas.hardware_watchdog_handoff", REQUESTED);

/// Whether this kernel still feeds the hardware watchdog.
///
/// The software reboot path clears it before asking SBI to reset. If SBI has
/// no reset extension (or the call fails), the un-fed watchdog is what still
/// brings the board back: it is the only layer left when the kernel cannot
/// complete a software reset.
static KICK_FAILED_REPORTED: AtomicBool = AtomicBool::new(false);

static KICKING: AtomicBool = AtomicBool::new(true);

/// Stops feeding the hardware watchdog for the rest of this boot.
///
/// Called immediately before a software reset is attempted. If the reset
/// works the watchdog never fires; if it does not, the watchdog resets the
/// SoC within its programmed timeout instead of leaving the board halted.
pub(crate) fn stop_kicking_for_reset() {
    KICKING.store(false, Ordering::Release);
}

/// Whether this kernel still feeds the hardware watchdog.
///
/// `/proc/sys/kernel/asterinas_watchdog_drill` reports it, so a drill run can
/// be told apart from a board that merely stopped booting.
pub(crate) fn is_kicking() -> bool {
    KICKING.load(Ordering::Acquire)
}

fn requested() -> bool {
    REQUESTED
        .get()
        .is_some_and(|value| value == "1" || value == "on" || value == "true")
}

fn has_designware_watchdog() -> bool {
    DEVICE_TREE.get().is_some_and(|tree| {
        tree.all_nodes().any(|node| {
            node.compatible()
                .is_some_and(|compatible| compatible.all().any(|item| item == "snps,dw-wdt"))
        })
    })
}

/// Adopt the watchdog, arming it when U-Boot left it alone.
///
/// A boot that does not arm the block used to end up with no hardware
/// recovery at all, which is how a wedged board came to need a power cycle.
/// The kernel now takes the block out of reset itself and enables it, so the
/// fail-safe exists on every boot, not only on the ones whose `bootcmd` was
/// written by hand.
pub(super) fn handoff_if_requested() {
    if !requested() {
        return;
    }
    if !has_designware_watchdog() {
        ostd::early_println!(
            "ASTERINAS_HARDWARE_WATCHDOG_HANDOFF status=skip reason=missing-dw-wdt"
        );
        return;
    }

    let Ok(registers) = IoMem::acquire(WATCHDOG_BASE..WATCHDOG_BASE + WATCHDOG_LIMIT) else {
        ostd::early_println!(
            "ASTERINAS_HARDWARE_WATCHDOG_HANDOFF status=fail reason=mmio-unavailable"
        );
        return;
    };
    // The block reads `COMP_TYPE` as zero while the SoC keeps it in reset, so
    // release that reset before deciding anything about the signature. U-Boot's
    // arming sequence does the same write through the syscfg window.
    match IoMem::acquire(EIC7700_WDT_RESET_CONTROL..EIC7700_WDT_RESET_CONTROL + 4) {
        Ok(syscfg) => {
            let _ = syscfg.write_once(0, &EIC7700_WDT_RESET_RELEASE);
        }
        Err(_) => ostd::early_println!(
            "ASTERINAS_HARDWARE_WATCHDOG_HANDOFF status=warn reason=syscfg-unavailable"
        ),
    }
    let Ok(component) = registers.read_once::<u32>(COMPONENT_TYPE_OFFSET) else {
        ostd::early_println!(
            "ASTERINAS_HARDWARE_WATCHDOG_HANDOFF status=fail reason=component-read"
        );
        return;
    };
    let Ok(control) = registers.read_once::<u32>(CONTROL_OFFSET) else {
        ostd::early_println!("ASTERINAS_HARDWARE_WATCHDOG_HANDOFF status=fail reason=control-read");
        return;
    };
    if component != COMPONENT_TYPE {
        ostd::early_println!(
            "ASTERINAS_HARDWARE_WATCHDOG_HANDOFF status=fail reason=component-mismatch value={:#x}",
            component
        );
        return;
    }
    if control & CONTROL_ENABLE == 0 {
        // U-Boot did not arm it. Program the longest timeout the counter has
        // (TOP is four bits, so `2^(16+15)` clocks is the ceiling) and enable
        // it before the heartbeat below begins: from here on a kernel that
        // stops kicking is a board that resets itself.
        let _ = registers.write_once(TOP_OFFSET, &TOP_MAX);
        let _ = registers.write_once(COUNTER_RESTART_OFFSET, &COUNTER_RESTART_VALUE);
        let _ = registers.write_once(CONTROL_OFFSET, &CONTROL_ARM_WITH_RESET);
        ostd::early_println!("ASTERINAS_HARDWARE_WATCHDOG_HANDOFF status=armed-by-kernel");
    } else {
        // U-Boot armed the block itself, and the boot script arms it *before*
        // it loads anything: PHY negotiation, DHCP and a multi-megabyte kernel
        // read all come out of the same window, so the counter can be close to
        // expiring by the time this code runs. Widen TOP to the maximum and
        // restart the counter now; the periodic kick below would otherwise not
        // land for another KICK_PERIOD.
        let _ = registers.write_once(TOP_OFFSET, &TOP_MAX);
        let _ = registers.write_once(COUNTER_RESTART_OFFSET, &COUNTER_RESTART_VALUE);
    }

    // The board can reset without any panic, trace or watchdog-kick failure
    // if this callback simply stops being scheduled while the rest of the
    // system keeps running. A periodic line makes "the kicks stopped" visible
    // instead of inferred.
    static KICK_COUNT: AtomicU32 = AtomicU32::new(0);

    let heartbeat = move || {
        if !KICKING.load(Ordering::Acquire) {
            return;
        }
        let kicks = KICK_COUNT.fetch_add(1, Ordering::Relaxed) + 1;
        if kicks % HEARTBEAT_REPORT_EVERY == 0 {
            let (received, sent, waits) = ostd::smp::ipi_counters();
            ostd::early_println!(
                "ASTERINAS_WATCHDOG_HEARTBEAT kicks={} cpu={} ipi_rx={} ipi_tx={} ipi_wait={}",
                kicks,
                u32::from(ostd::cpu::CpuId::current_racy()),
                received,
                sent,
                waits
            );
        }
        // A failed write is intentionally ignored: if this MMIO path becomes
        // unavailable the hardware watchdog remains the fail-safe. Say it once,
        // so a board that resets itself leaves a trace of the last kick.
        if registers
            .write_once(COUNTER_RESTART_OFFSET, &COUNTER_RESTART_VALUE)
            .is_err()
            && !KICK_FAILED_REPORTED.swap(true, Ordering::Relaxed)
        {
            ostd::early_println!("ASTERINAS_WATCHDOG_KICK status=failed");
        }
        timer::request_interrupt_after(KICK_PERIOD);
    };
    timer::register_high_resolution_callback_on_cpu(heartbeat);
    timer::request_interrupt_after(KICK_PERIOD);
    ostd::early_println!("ASTERINAS_HARDWARE_WATCHDOG_HANDOFF status=active period_seconds=1");
}

#[cfg(ktest)]
mod tests {
    use ostd::prelude::ktest;

    use super::*;

    #[ktest]
    fn fixed_register_contract_matches_uboot() {
        assert_eq!(WATCHDOG_BASE, 0x5080_0000);
        assert_eq!(COMPONENT_TYPE_OFFSET, 0xfc);
        assert_eq!(COUNTER_RESTART_OFFSET, 0x0c);
        assert_eq!(COUNTER_RESTART_VALUE, 0x76);
        assert_eq!(COMPONENT_TYPE, 0x4457_0120);
        assert_eq!(CONTROL_ENABLE, 1);
    }
}
