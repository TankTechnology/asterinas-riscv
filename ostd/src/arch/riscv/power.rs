// SPDX-License-Identifier: MPL-2.0

//! Power management.

use crate::{
    io::IoMemAllocatorBuilder,
    power::{ExitCode, inject_poweroff_handler, inject_restart_handler},
};

mod qemu_test_finisher {
    use spin::Once;

    use crate::{
        arch::{boot::DEVICE_TREE, irq::disable_local_and_halt},
        io::{IoMem, IoMemAllocatorBuilder, Sensitive},
        mm::CachePolicy,
        power::ExitCode,
    };

    // QEMU's SiFive test device uses the low halfword as a command and the high
    // halfword as the host exit status. Use the same failure status as x86's
    // isa-debug-exit device so OSDK can distinguish it from QEMU startup errors.
    // Reference: https://github.com/qemu/qemu/blob/v10.0.0/hw/misc/sifive_test.c.
    const FINISHER_PASS: u32 = 0x5555;
    const FINISHER_FAIL: u32 = 0x3333;
    const HOST_FAILURE_STATUS: u32 = (0x20 << 1) | 1;

    static FINISHER: Once<IoMem<Sensitive>> = Once::new();

    pub(super) fn try_exit(code: &ExitCode) {
        let Some(finisher) = FINISHER.get() else {
            return;
        };
        let value = match code {
            ExitCode::Success => FINISHER_PASS,
            ExitCode::Failure => (HOST_FAILURE_STATUS << 16) | FINISHER_FAIL,
        };

        let _irq_guard = crate::irq::disable_local();
        // SAFETY: Initialization reserves the FDT-described sifive,test1 device
        // with at least one aligned u32 register. Offset zero is its finisher.
        unsafe { finisher.write_once(0, &value) };
        // Do not fall through to SBI: its syscon-poweroff write could overwrite
        // the failure status with FINISHER_PASS before QEMU finishes shutting down.
        disable_local_and_halt();
    }

    pub(super) fn init(io_mem_builder: &IoMemAllocatorBuilder) {
        let Some(node) = DEVICE_TREE
            .get()
            .and_then(|fdt| fdt.find_compatible(&["sifive,test1"]))
        else {
            return;
        };
        if !matches!(
            node.property("status").and_then(|status| status.as_str()),
            None | Some("ok" | "okay")
        ) {
            return;
        }
        let Some(reg) = node.reg().and_then(|mut regs| regs.next()) else {
            return;
        };
        let base = reg.starting_address as usize;
        let Some(size) = reg.size else {
            return;
        };
        let Some(end) = base.checked_add(size) else {
            return;
        };
        if !base.is_multiple_of(align_of::<u32>()) || size < size_of::<u32>() {
            return;
        }

        FINISHER.call_once(|| io_mem_builder.reserve(base..end, CachePolicy::Uncacheable));
    }
}

fn try_poweroff(code: ExitCode) {
    qemu_test_finisher::try_exit(&code);
    let _ = match code {
        ExitCode::Success => sbi_rt::system_reset(sbi_rt::Shutdown, sbi_rt::NoReason),
        ExitCode::Failure => sbi_rt::system_reset(sbi_rt::Shutdown, sbi_rt::SystemFailure),
    };
}

fn try_restart(code: ExitCode) {
    let _ = match code {
        ExitCode::Success => sbi_rt::system_reset(sbi_rt::ColdReboot, sbi_rt::NoReason),
        ExitCode::Failure => sbi_rt::system_reset(sbi_rt::ColdReboot, sbi_rt::SystemFailure),
    };
}

pub(super) fn init(io_mem_builder: &IoMemAllocatorBuilder) {
    qemu_test_finisher::init(io_mem_builder);
    inject_poweroff_handler(try_poweroff);
    inject_restart_handler(try_restart);
}
