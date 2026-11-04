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
use core::time::Duration;

use aster_cmdline::define_kv_param;
use ostd::{arch::boot::DEVICE_TREE, io::IoMem, mm::io::VmIoOnce, timer};
use spin::Once;

const WATCHDOG_BASE: usize = 0x5080_0000;
const WATCHDOG_LIMIT: usize = 0x100;
const CONTROL_OFFSET: usize = 0x00;
const COUNTER_RESTART_OFFSET: usize = 0x0c;
const COMPONENT_TYPE_OFFSET: usize = 0xfc;
const CONTROL_ENABLE: u32 = 1;
const COUNTER_RESTART_VALUE: u32 = 0x76;
const COMPONENT_TYPE: u32 = 0x4457_0120;
const KICK_PERIOD: Duration = Duration::from_secs(1);

static REQUESTED: Once<String> = Once::new();
define_kv_param!("asterinas.hardware_watchdog_handoff", REQUESTED);

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

/// Adopt a watchdog already armed by U-Boot, if the selected physical boot
/// explicitly requested the handoff. QEMU and ordinary boots are no-ops.
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
            "ASTERINAS_HARDWARE_WATCHDOG_HANDOFF status=fail reason=component-mismatch"
        );
        return;
    }
    if control & CONTROL_ENABLE == 0 {
        ostd::early_println!("ASTERINAS_HARDWARE_WATCHDOG_HANDOFF status=fail reason=not-armed");
        return;
    }

    let heartbeat = move || {
        // A failed write is intentionally ignored: if this MMIO path becomes
        // unavailable the hardware watchdog remains the fail-safe.
        let _ = registers.write_once(COUNTER_RESTART_OFFSET, &COUNTER_RESTART_VALUE);
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
