// SPDX-License-Identifier: MPL-2.0

//! NS16550A UART console support.

use alloc::string::ToString;

use fdt::node::FdtNode;
use fdt_util::AcquireIoMems;
use ostd::{
    arch::irq::{self as arch_irq, DeferredMappedIrqLine},
    console::uart_ns16650a::{Ns16550aAccess, Ns16550aRegister, Ns16550aUart},
    io::IoMem,
    irq::IrqLine,
    mm::VmIoOnce,
    sync::{LocalIrqDisabled, SpinLock},
};

use crate::console::{Uart, UartConsole};

pub(super) const FDT_COMPATIBLES: [&str; 1] = ["ns16550a"];

/// Access to serial registers via `IoMem`.
struct SerialAccess {
    io_mem: IoMem,
}

impl Ns16550aAccess for SerialAccess {
    fn read(&self, reg: Ns16550aRegister) -> u8 {
        self.io_mem.read_once(reg as u16 as usize).unwrap()
    }

    fn write(&mut self, reg: Ns16550aRegister, val: u8) {
        self.io_mem.write_once(reg as u16 as usize, &val).unwrap();
    }
}

/// IRQ line for UART serial, kept so the top half can re-arm it after draining.
///
/// A PLIC source is level-sensitive: if the handler returns without removing
/// the device's reason to assert the line, the gateway re-raises immediately
/// and the hart never leaves the claim loop. The line is therefore mapped
/// masked, masked again on every delivery, and re-armed only once the receive
/// FIFO has been drained.
static IRQ_LINE: SpinLock<Option<DeferredMappedIrqLine>, LocalIrqDisabled> = SpinLock::new(None);

pub(super) fn init(fdt_node: FdtNode) {
    let Some([io_mem]) =
        fdt_node.acquire_io_mems([const { Ns16550aRegister::MAX as u16 as usize + 1 }])
    else {
        return;
    };

    let interrupt_source = match super::parse_explicit_interrupt_source(fdt_node) {
        Ok(source) => source,
        Err(error) => {
            ostd::info!("invalid NS16550A interrupt source: {:?}", error);
            return;
        }
    };

    let Ok(mut irq_line) = IrqLine::alloc().and_then(|irq_line| {
        arch_irq::IRQ_CHIP
            .get()
            .unwrap()
            .map_fdt_pin_to_masked(interrupt_source, irq_line)
    }) else {
        ostd::info!("IRQ line is not available for NS16550A");
        return;
    };

    let mut uart = Ns16550aUart::new(SerialAccess { io_mem });
    uart.init();
    // A break must interrupt even when no data byte follows it: that is the
    // serial SysRq entry point when userspace can no longer run anything.
    uart.enable_break_detection();

    let uart_console = UartConsole::new(SpinLock::new(uart));

    aster_console::register_device(
        aster_console::UART_CONSOLE_NAME.to_string(),
        uart_console.clone(),
    );

    let cloned_uart_console = uart_console.clone();
    if irq_line
        .on_active_and_mask(move |_| {
            cloned_uart_console.trigger_input_callbacks();
            // Nothing is deferred: reading the FIFO has already removed the
            // device's reason to assert the line, so re-arm straight away.
            if let Some(line) = IRQ_LINE.lock().as_mut() {
                if line.rearm().is_err() {
                    ostd::warn!("NS16550A: failed to re-arm the interrupt source");
                }
            }
        })
        .is_err()
    {
        ostd::info!("could not register the NS16550A interrupt handler");
        return;
    }
    *IRQ_LINE.lock() = Some(irq_line);
    // The source was mapped masked; open it once the handler is in place.
    if let Some(line) = IRQ_LINE.lock().as_mut() {
        let _ = line.rearm();
    }
    uart_console.uart().flush();

    ostd::info!("Registered NS16550A as a console");
}
