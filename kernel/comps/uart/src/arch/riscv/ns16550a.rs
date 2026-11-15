// SPDX-License-Identifier: MPL-2.0

//! NS16550A UART console support.

use alloc::string::ToString;

use fdt::node::FdtNode;
use fdt_util::AcquireIoMems;
use ostd::{
    arch::irq::{self as arch_irq, MappedIrqLine},
    console::uart_ns16650a::{Ns16550aAccess, Ns16550aRegister, Ns16550aUart},
    io::IoMem,
    irq::IrqLine,
    mm::VmIoOnce,
    sync::SpinLock,
};
use spin::Once;

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

/// IRQ line for UART serial.
static IRQ_LINE: Once<MappedIrqLine> = Once::new();

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
            .map_fdt_pin_to(interrupt_source, irq_line)
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
    irq_line.on_active(move |_| cloned_uart_console.trigger_input_callbacks());
    IRQ_LINE.call_once(move || irq_line);
    uart_console.uart().flush();

    ostd::info!("Registered NS16550A as a console");
}
