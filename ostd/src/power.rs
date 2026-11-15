// SPDX-License-Identifier: MPL-2.0

//! Power management.

use spin::Once;

use crate::{arch::irq::disable_local_and_halt, cpu::CpuSet};

/// An exit code that denotes the reason for restarting or powering off.
///
/// Whether or not the code is used depends on the hardware. In a virtualization environment, it
/// can be passed to the hypervisor (e.g., as QEMU's exit code). In a bare-metal environment, it
/// can be passed to the firmware. In either case, the code may be silently ignored if reporting
/// the code is not supported.
#[derive(Clone, Copy)]
pub enum ExitCode {
    /// The code that indicates a successful exit.
    Success,
    /// The code that indicates a failed exit.
    Failure,
}

static RESTART_HANDLER: Once<fn(ExitCode)> = Once::new();

/// Injects a handler that can restart the system.
///
/// If no handler is injected, [`restart`] calls [`crate::arch::power::try_restart`].
/// Injection replaces that behavior: [`restart`] invokes only the injected handler, which may call
/// the architecture-specific operation as part of its own policy.
///
/// ```no_run
/// use ostd::power::{self, ExitCode};
///
/// fn init() {
///     power::inject_restart_handler(restart_policy);
/// }
///
/// fn restart_policy(code: ExitCode) {
///     try_platform_restart();
///     ostd::arch::power::try_restart(code);
/// }
///
/// fn try_platform_restart() {
///     // Try a platform-specific restart mechanism, if available.
/// }
/// ```
///
/// The function may be called only once; subsequent calls take no effect.
pub fn inject_restart_handler(handler: fn(ExitCode)) {
    RESTART_HANDLER.call_once(|| handler);
}

/// Restarts the system.
///
/// This function will not return. If the restart mechanism fails, this function will halt all CPUs
/// on the machine.
pub fn restart(code: ExitCode) -> ! {
    if let Some(handler) = RESTART_HANDLER.get() {
        (handler)(code);
    } else {
        crate::arch::power::try_restart(code);
    }
    crate::error!("Failed to restart the system");

    machine_halt();
}

/// Restarts the system without logging, cleanup, or cross-CPU coordination.
///
/// This function is suitable for interrupt and fatal-panic contexts. It invokes the installed
/// restart handler once. If the handler is missing or returns, the current CPU disables local
/// interrupts and halts forever.
pub fn emergency_restart(code: ExitCode) -> ! {
    if let Some(handler_fn) = RESTART_HANDLER.get() {
        handler_fn(code);
    }

    disable_local_and_halt();
}

static POWEROFF_HANDLER: Once<fn(ExitCode)> = Once::new();

/// Injects a handler that can power off the system.
///
/// If no handler is injected, [`poweroff`] calls [`crate::arch::power::try_poweroff`].
/// Injection replaces that behavior: [`poweroff`] invokes only the injected handler, which may call
/// the architecture-specific operation as part of its own policy.
///
/// ```no_run
/// use ostd::power::{self, ExitCode};
///
/// fn init() {
///     power::inject_poweroff_handler(poweroff_policy);
/// }
///
/// fn poweroff_policy(code: ExitCode) {
///     try_platform_poweroff();
///     ostd::arch::power::try_poweroff(code);
/// }
///
/// fn try_platform_poweroff() {
///     // Try a platform-specific poweroff mechanism, if available.
/// }
/// ```
///
/// The function may be called only once; subsequent calls take no effect.
pub fn inject_poweroff_handler(handler: fn(ExitCode)) {
    POWEROFF_HANDLER.call_once(|| handler);
}

/// Powers off the system.
///
/// This function will not return. If the poweroff mechanism fails, this function will halt all CPUs
/// on the machine.
pub fn poweroff(code: ExitCode) -> ! {
    #[cfg(feature = "coverage")]
    crate::coverage::on_system_exit();

    if let Some(handler) = POWEROFF_HANDLER.get() {
        (handler)(code);
        log_shutdown("failed to power off the system because the poweroff handler fails");
    } else {
        crate::arch::power::try_poweroff(code);
        log_shutdown("failed to power off the system because a poweroff handler is missing");
    }

    machine_halt();
}

/// Powers off the system without logging, cleanup, or cross-CPU coordination.
///
/// This function is suitable for interrupt and fatal-panic contexts, matching
/// [`emergency_restart`]. If the handler is missing or returns, the current
/// CPU disables local interrupts and halts forever.
pub fn emergency_poweroff(code: ExitCode) -> ! {
    if let Some(handler_fn) = POWEROFF_HANDLER.get() {
        handler_fn(code);
    }

    disable_local_and_halt();
}

fn machine_halt() -> ! {
    log_shutdown("halting the machine...");

    // TODO: `inter_processor_call` may panic again (e.g., if there is an out-of-memory error). We
    // should find a way to make it panic-free.
    if let Some(ipi_sender) = crate::smp::IPI_SENDER.get() {
        ipi_sender.inter_processor_call(&CpuSet::new_full(), || disable_local_and_halt());
    }
    disable_local_and_halt();
}

fn log_shutdown(message: &str) {
    // RISC-V UART failures can reach this path while the generic logger is
    // locked. Its SBI-backed early console remains available independently.
    #[cfg(target_arch = "riscv64")]
    crate::early_println!("{}", message);
    #[cfg(not(target_arch = "riscv64"))]
    crate::error!("{}", message);
}
