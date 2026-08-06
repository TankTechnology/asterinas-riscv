// SPDX-License-Identifier: MPL-2.0

use alloc::{string::ToString, sync::Arc};
use core::{
    ops::Range,
    sync::atomic::{AtomicBool, Ordering},
};

use aster_console::{ConsoleSendError, ConsoleSendReadyError};
use aster_softirq::Taskless;
use fdt::node::FdtNode;
use ostd::{
    arch::irq::{self as arch_irq, MappedIrqLine},
    io::IoMem,
    irq::IrqLine,
    mm::VmIoOnce,
};
use spin::Once;

use super::ExplicitInterruptSourceError;
use crate::{
    CONSOLE_NAME,
    console::{DiagnosticSendError, Uart, UartConsole},
};

const REGISTER_SHIFT: usize = 2;
const REGISTER_IO_WIDTH: usize = size_of::<u32>();
const RBR_OFFSET: usize = 0 << REGISTER_SHIFT;
const THR_OFFSET: usize = 0 << REGISTER_SHIFT;
const IER_OFFSET: usize = 1 << REGISTER_SHIFT;
const IIR_OFFSET: usize = 2 << REGISTER_SHIFT;
const LSR_OFFSET: usize = 5 << REGISTER_SHIFT;
const USR_OFFSET: usize = 31 << REGISTER_SHIFT;
const REQUIRED_MMIO_SIZE: usize = USR_OFFSET + REGISTER_IO_WIDTH;
// These standard 8250 bit and interrupt-ID definitions are documented in
// <https://github.com/torvalds/linux/blob/master/include/uapi/linux/serial_reg.h>.
// DW's Busy Detect ID and USR acknowledgement are specified in Table 8 of
// <https://linux-sunxi.org/images/d/d2/Dw_apb_uart_db.pdf>.
const IER_RDI: u32 = 1 << 0;
const IER_UNHANDLED_STANDARD_SOURCES: u32 = 0b1110;
const IIR_ID_MASK: u32 = 0x0f;
const IIR_MODEM_STATUS: u32 = 0x00;
const IIR_NO_INTERRUPT: u32 = 0x01;
const IIR_THRE: u32 = 0x02;
const IIR_RDI: u32 = 0x04;
const IIR_RLSI: u32 = 0x06;
const IIR_BUSY: u32 = 0x07;
const IIR_CTI: u32 = 0x0c;
const LSR_DR: u32 = 1 << 0;
const LSR_THRE: u32 = 1 << 5;
// This is one total budget per probe or buffer, not a fresh budget for every byte.
const TX_POLL_BUDGET: usize = 100_000;
const RX_FLUSH_BUDGET: usize = 64;
const RX_BATCH_BUDGET: usize = 4;
const RX_QUIESCE_CAUSE_BUDGET: usize = 4;

static IRQ_LINE: Once<MappedIrqLine> = Once::new();
static RX_TASKLESS: Once<Arc<Taskless>> = Once::new();

trait DwApbAccess {
    fn read32(&self, offset: usize) -> Result<u32, ()>;

    fn write32(&self, offset: usize, value: u32) -> Result<(), ()>;
}

struct IoMemAccess {
    io_mem: IoMem,
}

impl DwApbAccess for IoMemAccess {
    fn read32(&self, offset: usize) -> Result<u32, ()> {
        self.io_mem.read_once::<u32>(offset).map_err(|_| ())
    }

    fn write32(&self, offset: usize, value: u32) -> Result<(), ()> {
        self.io_mem.write_once(offset, &value).map_err(|_| ())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TxError {
    Busy,
    Mmio,
    TimedOut,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RxEnableError {
    Mmio,
    UnsupportedInterruptSources,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct PreparedRxInterrupt(u32);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RxInterruptCause {
    None,
    Receive,
    BusyCleared,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RxServiceError {
    Mmio,
    UnsupportedCause(u32),
    StillPending,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RxDeferError {
    Service(RxServiceError),
    Mask(RxEnableError),
}

struct TxOwnerGuard<'a> {
    tx_owned: &'a AtomicBool,
}

impl Drop for TxOwnerGuard<'_> {
    fn drop(&mut self) {
        self.tx_owned.store(false, Ordering::Release);
    }
}

struct DwApbUart<A> {
    access: A,
    tx_owned: AtomicBool,
}

impl<A: DwApbAccess> DwApbUart<A> {
    fn new(access: A) -> Self {
        Self {
            access,
            tx_owned: AtomicBool::new(false),
        }
    }

    fn try_send_with_budget(&self, buf: &[u8], poll_budget: usize) -> Result<(), TxError> {
        let _owner = self.try_claim_tx()?;
        let mut remaining_polls = poll_budget;

        for &byte in buf {
            if byte == b'\n' {
                self.send_byte(b'\r', &mut remaining_polls)?;
            }
            self.send_byte(byte, &mut remaining_polls)?;
        }

        Ok(())
    }

    fn try_send_tty_with_budget(
        &self,
        buf: &[u8],
        poll_budget: usize,
    ) -> Result<usize, ConsoleSendError> {
        if buf.is_empty() {
            return Ok(0);
        }
        let mut remaining_polls = poll_budget;
        let _owner = self
            .try_claim_tx()
            .map_err(|_error| ConsoleSendError::Busy)?;
        for (index, &byte) in buf.iter().enumerate() {
            if self.send_byte(byte, &mut remaining_polls).is_err() {
                return if index == 0 {
                    Err(ConsoleSendError::Io)
                } else {
                    Ok(index)
                };
            }
        }

        Ok(buf.len())
    }

    fn try_claim_tx(&self) -> Result<TxOwnerGuard<'_>, TxError> {
        self.tx_owned
            .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
            .map_err(|_| TxError::Busy)?;
        Ok(TxOwnerGuard {
            tx_owned: &self.tx_owned,
        })
    }

    fn send_byte(&self, byte: u8, remaining_polls: &mut usize) -> Result<(), TxError> {
        wait_ready(&self.access, remaining_polls)?;
        self.access
            .write32(THR_OFFSET, u32::from(byte))
            .map_err(|_| TxError::Mmio)
    }

    fn receive_available(&self, buf: &mut [u8]) -> usize {
        for (index, byte) in buf.iter_mut().enumerate() {
            let Ok(line_status) = self.access.read32(LSR_OFFSET) else {
                return index;
            };
            if line_status & LSR_DR == 0 {
                return index;
            }

            let Ok(data) = self.access.read32(RBR_OFFSET) else {
                return index;
            };
            *byte = data.to_le_bytes()[0];
        }

        buf.len()
    }

    fn prepare_rx_interrupt(&self) -> Result<PreparedRxInterrupt, RxEnableError> {
        let interrupt_enable = self
            .access
            .read32(IER_OFFSET)
            .map_err(|_| RxEnableError::Mmio)?;
        if interrupt_enable & IER_UNHANDLED_STANDARD_SOURCES != 0 {
            return Err(RxEnableError::UnsupportedInterruptSources);
        }

        Ok(PreparedRxInterrupt(interrupt_enable | IER_RDI))
    }

    fn enable_rx_interrupt(&self, prepared: PreparedRxInterrupt) -> Result<(), RxEnableError> {
        self.access
            .write32(IER_OFFSET, prepared.0)
            .map_err(|_| RxEnableError::Mmio)
    }

    fn disable_rx_interrupt(&self) -> Result<(), RxEnableError> {
        let interrupt_enable = self
            .access
            .read32(IER_OFFSET)
            .map_err(|_| RxEnableError::Mmio)?;
        self.access
            .write32(IER_OFFSET, interrupt_enable & !IER_RDI)
            .map_err(|_| RxEnableError::Mmio)
    }

    fn acknowledge_interrupt_cause(&self) -> Result<RxInterruptCause, RxServiceError> {
        let interrupt_id = self
            .access
            .read32(IIR_OFFSET)
            .map_err(|_| RxServiceError::Mmio)?
            & IIR_ID_MASK;

        match interrupt_id {
            IIR_NO_INTERRUPT => Ok(RxInterruptCause::None),
            IIR_RDI | IIR_CTI => Ok(RxInterruptCause::Receive),
            IIR_RLSI => {
                self.access
                    .read32(LSR_OFFSET)
                    .map_err(|_| RxServiceError::Mmio)?;
                Ok(RxInterruptCause::Receive)
            }
            IIR_BUSY => {
                self.access
                    .read32(USR_OFFSET)
                    .map_err(|_| RxServiceError::Mmio)?;
                Ok(RxInterruptCause::BusyCleared)
            }
            IIR_MODEM_STATUS | IIR_THRE => Err(RxServiceError::UnsupportedCause(interrupt_id)),
            _ => Err(RxServiceError::UnsupportedCause(interrupt_id)),
        }
    }

    fn quiesce_rx_interrupt(&self) -> Result<(), RxServiceError> {
        let mut discarded = [0; RX_FLUSH_BUDGET];
        let mut receive_budget_available = true;

        for _ in 0..RX_QUIESCE_CAUSE_BUDGET {
            match self.acknowledge_interrupt_cause()? {
                RxInterruptCause::None => return Ok(()),
                RxInterruptCause::BusyCleared => {}
                RxInterruptCause::Receive if receive_budget_available => {
                    self.receive_available(&mut discarded);
                    receive_budget_available = false;
                }
                RxInterruptCause::Receive => return Err(RxServiceError::StillPending),
            }
        }

        Err(RxServiceError::StillPending)
    }

    fn prepare_deferred_rx(&self) -> Result<bool, RxDeferError> {
        if self
            .acknowledge_interrupt_cause()
            .map_err(RxDeferError::Service)?
            != RxInterruptCause::Receive
        {
            return Ok(false);
        }

        self.disable_rx_interrupt().map_err(RxDeferError::Mask)?;
        Ok(true)
    }
}

impl<A: DwApbAccess> Uart for DwApbUart<A> {
    fn try_send_diagnostic(&self, buf: &[u8]) -> Result<(), DiagnosticSendError> {
        match self.try_send_with_budget(buf, TX_POLL_BUDGET) {
            Ok(()) | Err(TxError::Busy | TxError::TimedOut) => Ok(()),
            Err(TxError::Mmio) => Err(DiagnosticSendError::Io),
        }
    }

    fn try_send_tty(&self, buf: &[u8]) -> Result<usize, ConsoleSendError> {
        self.try_send_tty_with_budget(buf, TX_POLL_BUDGET)
    }

    fn poll_send_ready(&self) -> Result<bool, ConsoleSendReadyError> {
        Ok(!self.tx_owned.load(Ordering::Acquire))
    }

    fn recv(&self, buf: &mut [u8]) -> usize {
        self.receive_available(buf)
    }

    fn flush(&self) {
        let mut discarded = [0; RX_FLUSH_BUDGET];
        let _ = self.receive_available(&mut discarded);
    }
}

fn wait_ready<A: DwApbAccess>(access: &A, remaining_polls: &mut usize) -> Result<(), TxError> {
    while *remaining_polls > 0 {
        *remaining_polls -= 1;
        let line_status = access.read32(LSR_OFFSET).map_err(|_| TxError::Mmio)?;
        if line_status & LSR_THRE != 0 {
            return Ok(());
        }
    }

    Err(TxError::TimedOut)
}

fn probe_ready<A: DwApbAccess>(access: &A, poll_budget: usize) -> Result<(), TxError> {
    let mut remaining_polls = poll_budget;
    wait_ready(access, &mut remaining_polls)
}

fn prepare_for_registration<A: DwApbAccess>(
    access: A,
    poll_budget: usize,
) -> Result<DwApbUart<A>, TxError> {
    probe_ready(&access, poll_budget)?;
    Ok(DwApbUart::new(access))
}

pub(super) fn init(fdt_node: FdtNode) {
    let config = match DwApbConfig::from_node(fdt_node) {
        Ok(config) => config,
        Err(error) => {
            ostd::warn!("failed to validate DW APB UART: {:?}", error);
            return;
        }
    };
    let Ok(io_mem) = IoMem::acquire(config.mmio_range()) else {
        ostd::error!("I/O memory is not available for DW APB UART");
        return;
    };
    let uart = match prepare_for_registration(IoMemAccess { io_mem }, TX_POLL_BUDGET) {
        Ok(uart) => uart,
        Err(error) => {
            ostd::error!("DW APB UART readiness probe failed: {:?}", error);
            return;
        }
    };

    let uart_console = UartConsole::new(uart);
    aster_console::register_device(CONSOLE_NAME.to_string(), uart_console.clone());

    ostd::info!("Registered DW APB UART as a console");

    match try_initialize_rx_path(fdt_node, uart_console) {
        Ok(()) => ostd::info!("Enabled DW APB UART receive interrupt"),
        Err(error) => ostd::warn!(
            "failed to initialize DW APB UART receive path; TX remains available: {:?}",
            error
        ),
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DwApbConfigError {
    Disabled,
    InvalidStatus,
    MissingReg,
    MissingRegSize,
    MissingRegShift,
    UnsupportedRegShift,
    MissingRegIoWidth,
    UnsupportedRegIoWidth,
    MmioRangeTooSmall,
    MmioRangeOverflow,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct DwApbConfig {
    mmio_range: Range<usize>,
}

impl DwApbConfig {
    fn from_node(fdt_node: FdtNode) -> Result<Self, DwApbConfigError> {
        let status = match fdt_node.property("status") {
            Some(property) => Some(property.as_str().ok_or(DwApbConfigError::InvalidStatus)?),
            None => None,
        };
        let reg_shift = fdt_node
            .property("reg-shift")
            .and_then(|prop| prop.as_usize());
        let reg_io_width = fdt_node
            .property("reg-io-width")
            .and_then(|prop| prop.as_usize());
        let reg = fdt_node
            .reg()
            .and_then(|mut regs| regs.next())
            .ok_or(DwApbConfigError::MissingReg)?;
        let reg_size = reg.size.ok_or(DwApbConfigError::MissingRegSize)?;

        Self::validate(
            status,
            reg_shift,
            reg_io_width,
            reg.starting_address as usize,
            reg_size,
        )
    }

    fn validate(
        status: Option<&str>,
        reg_shift: Option<usize>,
        reg_io_width: Option<usize>,
        reg_base: usize,
        reg_size: usize,
    ) -> Result<Self, DwApbConfigError> {
        if !matches!(status, None | Some("ok" | "okay")) {
            return Err(DwApbConfigError::Disabled);
        }

        match reg_shift {
            None => return Err(DwApbConfigError::MissingRegShift),
            Some(REGISTER_SHIFT) => {}
            Some(_) => return Err(DwApbConfigError::UnsupportedRegShift),
        }

        match reg_io_width {
            None => return Err(DwApbConfigError::MissingRegIoWidth),
            Some(REGISTER_IO_WIDTH) => {}
            Some(_) => return Err(DwApbConfigError::UnsupportedRegIoWidth),
        }

        if reg_size < REQUIRED_MMIO_SIZE {
            return Err(DwApbConfigError::MmioRangeTooSmall);
        }
        let reg_end = reg_base
            .checked_add(reg_size)
            .ok_or(DwApbConfigError::MmioRangeOverflow)?;

        Ok(Self {
            mmio_range: reg_base..reg_end,
        })
    }

    fn mmio_range(&self) -> Range<usize> {
        self.mmio_range.clone()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RxInitError {
    InvalidConfig(ExplicitInterruptSourceError),
    IrqLineUnavailable,
    IrqChipUnavailable,
    QuiesceFailed(RxServiceError),
    MapFailed,
    EnableFailed(RxEnableError),
}

fn try_initialize_rx_path(
    fdt_node: FdtNode,
    uart_console: Arc<UartConsole<DwApbUart<IoMemAccess>>>,
) -> Result<(), RxInitError> {
    let interrupt_source =
        super::parse_explicit_interrupt_source(fdt_node).map_err(RxInitError::InvalidConfig)?;
    let prepared = uart_console
        .uart()
        .prepare_rx_interrupt()
        .map_err(RxInitError::EnableFailed)?;
    uart_console
        .uart()
        .quiesce_rx_interrupt()
        .map_err(RxInitError::QuiesceFailed)?;
    let mut irq_line = IrqLine::alloc().map_err(|_| RxInitError::IrqLineUnavailable)?;

    let taskless_console = uart_console.clone();
    RX_TASKLESS.call_once(|| Taskless::new(move || process_deferred_rx(&taskless_console)));
    let callback_console = uart_console.clone();
    irq_line.on_active(move |_| {
        if callback_console.uart().prepare_deferred_rx() == Ok(true) {
            RX_TASKLESS.get().unwrap().schedule_urgent();
        }
    });

    let irq_chip = arch_irq::IRQ_CHIP
        .get()
        .ok_or(RxInitError::IrqChipUnavailable)?;
    let mapped_irq_line = irq_chip
        .map_fdt_pin_to(interrupt_source, irq_line)
        .map_err(|_| RxInitError::MapFailed)?;

    uart_console
        .uart()
        .enable_rx_interrupt(prepared)
        .map_err(RxInitError::EnableFailed)?;
    IRQ_LINE.call_once(move || mapped_irq_line);

    Ok(())
}

fn process_deferred_rx(uart_console: &Arc<UartConsole<DwApbUart<IoMemAccess>>>) {
    if uart_console.trigger_input_callbacks_bounded(RX_BATCH_BUDGET) {
        RX_TASKLESS.get().unwrap().schedule_urgent();
        return;
    }

    let Ok(prepared) = uart_console.uart().prepare_rx_interrupt() else {
        return;
    };
    let _ = uart_console.uart().enable_rx_interrupt(prepared);
}

#[cfg(ktest)]
mod tests {
    use alloc::{collections::VecDeque, vec, vec::Vec};

    use ostd::prelude::*;
    use spin::Mutex;

    use super::*;

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum Operation {
        Read32(usize),
        Write32(usize, u32),
    }

    struct ScriptState {
        reads: VecDeque<Result<u32, ()>>,
        writes: VecDeque<Result<(), ()>>,
        operations: Vec<Operation>,
    }

    #[derive(Clone)]
    struct ScriptedAccess {
        state: Arc<Mutex<ScriptState>>,
    }

    impl ScriptedAccess {
        fn new<const N: usize>(reads: [Result<u32, ()>; N]) -> Self {
            Self::with_write_results(reads, [])
        }

        fn with_write_results<const N: usize, const M: usize>(
            reads: [Result<u32, ()>; N],
            writes: [Result<(), ()>; M],
        ) -> Self {
            Self {
                state: Arc::new(Mutex::new(ScriptState {
                    reads: reads.into_iter().collect(),
                    writes: writes.into_iter().collect(),
                    operations: Vec::new(),
                })),
            }
        }

        fn operations(&self) -> Vec<Operation> {
            self.state.lock().operations.clone()
        }

        fn read_count(&self) -> usize {
            self.state
                .lock()
                .operations
                .iter()
                .filter(|operation| matches!(operation, Operation::Read32(_)))
                .count()
        }

        fn writes(&self) -> Vec<(usize, u32)> {
            self.state
                .lock()
                .operations
                .iter()
                .filter_map(|operation| match operation {
                    Operation::Write32(offset, value) => Some((*offset, *value)),
                    Operation::Read32(_) => None,
                })
                .collect()
        }
    }

    impl DwApbAccess for ScriptedAccess {
        fn read32(&self, offset: usize) -> Result<u32, ()> {
            let mut state = self.state.lock();
            state.operations.push(Operation::Read32(offset));
            state.reads.pop_front().unwrap_or(Ok(0))
        }

        fn write32(&self, offset: usize, value: u32) -> Result<(), ()> {
            let mut state = self.state.lock();
            state.operations.push(Operation::Write32(offset, value));
            state.writes.pop_front().unwrap_or(Ok(()))
        }
    }

    #[ktest]
    fn dw_apb_accepts_the_megrez_contract() {
        let config =
            DwApbConfig::validate(Some("okay"), Some(2), Some(4), 0x5090_0000, 0x1_0000).unwrap();

        assert_eq!(config.mmio_range(), 0x5090_0000..0x5091_0000);
        assert_eq!(THR_OFFSET, 0);
        assert_eq!(LSR_OFFSET, 0x14);
    }

    #[ktest]
    fn dw_apb_accepts_absent_and_ok_status() {
        assert!(DwApbConfig::validate(None, Some(2), Some(4), 0x1000, REQUIRED_MMIO_SIZE).is_ok());
        assert!(
            DwApbConfig::validate(Some("ok"), Some(2), Some(4), 0x1000, REQUIRED_MMIO_SIZE,)
                .is_ok()
        );
    }

    #[ktest]
    fn dw_apb_rejects_missing_access_properties() {
        assert_eq!(
            DwApbConfig::validate(None, None, Some(4), 0x1000, 0x18),
            Err(DwApbConfigError::MissingRegShift)
        );
        assert_eq!(
            DwApbConfig::validate(None, Some(2), None, 0x1000, 0x18),
            Err(DwApbConfigError::MissingRegIoWidth)
        );
    }

    #[ktest]
    fn dw_apb_rejects_unsupported_access_layout() {
        assert_eq!(
            DwApbConfig::validate(Some("okay"), Some(0), Some(4), 0x1000, 0x18),
            Err(DwApbConfigError::UnsupportedRegShift)
        );
        assert_eq!(
            DwApbConfig::validate(Some("okay"), Some(2), Some(1), 0x1000, 0x18),
            Err(DwApbConfigError::UnsupportedRegIoWidth)
        );
    }

    #[ktest]
    fn dw_apb_rejects_disabled_status() {
        assert_eq!(
            DwApbConfig::validate(Some("disabled"), Some(2), Some(4), 0x1000, 0x18),
            Err(DwApbConfigError::Disabled)
        );
    }

    #[ktest]
    fn dw_apb_rejects_an_undersized_register_range() {
        assert_eq!(
            DwApbConfig::validate(None, Some(2), Some(4), 0x1000, REQUIRED_MMIO_SIZE - 1,),
            Err(DwApbConfigError::MmioRangeTooSmall)
        );
    }

    #[ktest]
    fn dw_apb_rejects_an_overflowing_register_range() {
        assert_eq!(
            DwApbConfig::validate(None, Some(2), Some(4), usize::MAX - 7, REQUIRED_MMIO_SIZE,),
            Err(DwApbConfigError::MmioRangeOverflow)
        );
    }

    #[ktest]
    fn dw_apb_probe_reads_only_the_shifted_lsr() {
        let access = ScriptedAccess::new([Ok(LSR_THRE)]);

        assert_eq!(probe_ready(&access, 3), Ok(()));
        assert_eq!(access.operations(), vec![Operation::Read32(0x14)]);
    }

    #[ktest]
    fn dw_apb_send_uses_shifted_u32_access_and_crlf() {
        let access = ScriptedAccess::new([Ok(LSR_THRE), Ok(LSR_THRE), Ok(LSR_THRE)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        assert_eq!(uart.try_send_with_budget(b"A\n", 3), Ok(()));
        assert_eq!(
            observation.writes(),
            vec![(THR_OFFSET, 0x41), (THR_OFFSET, 0x0d), (THR_OFFSET, 0x0a)]
        );
        assert!(!uart.tx_owned.load(Ordering::Relaxed));
    }

    #[ktest]
    fn dw_apb_send_stops_at_one_total_poll_budget() {
        let access = ScriptedAccess::new([Ok(0), Ok(0), Ok(0), Ok(LSR_THRE)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        assert_eq!(uart.try_send_with_budget(b"AB", 3), Err(TxError::TimedOut));
        assert_eq!(observation.read_count(), 3);
        assert!(observation.writes().is_empty());
    }

    #[ktest]
    fn dw_apb_send_does_not_restart_budget_after_a_prefix() {
        let access = ScriptedAccess::new([Ok(LSR_THRE), Ok(0), Ok(0)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        assert_eq!(uart.try_send_with_budget(b"AB", 3), Err(TxError::TimedOut));
        assert_eq!(observation.writes(), vec![(THR_OFFSET, 0x41)]);
        assert_eq!(observation.read_count(), 3);
    }

    #[ktest]
    fn dw_apb_send_stops_after_a_write_error() {
        let access = ScriptedAccess::with_write_results([Ok(LSR_THRE)], [Err(())]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        assert_eq!(uart.try_send_with_budget(b"AB", 2), Err(TxError::Mmio));
        assert_eq!(observation.read_count(), 1);
        assert_eq!(observation.writes(), vec![(THR_OFFSET, 0x41)]);
    }

    #[ktest]
    fn dw_apb_send_fails_immediately_when_owned() {
        let access = ScriptedAccess::new([Ok(LSR_THRE)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);
        uart.tx_owned.store(true, Ordering::Relaxed);

        assert_eq!(uart.try_send_with_budget(b"A", 10), Err(TxError::Busy));
        assert_eq!(observation.read_count(), 0);
    }

    #[ktest]
    fn dw_apb_tty_send_reports_a_stalled_owner_as_busy() {
        let uart = DwApbUart::new(ScriptedAccess::new([]));
        let _owner = uart.try_claim_tx().unwrap();

        assert_eq!(
            uart.try_send_tty_with_budget(b"A", 1),
            Err(ConsoleSendError::Busy)
        );
    }

    #[ktest]
    fn dw_apb_readiness_follows_transmitter_ownership() {
        let uart = DwApbUart::new(ScriptedAccess::new([]));

        assert_eq!(Uart::poll_send_ready(&uart), Ok(true));
        let owner = uart.try_claim_tx().unwrap();
        assert_eq!(Uart::poll_send_ready(&uart), Ok(false));
        drop(owner);
        assert_eq!(Uart::poll_send_ready(&uart), Ok(true));
    }

    #[ktest]
    fn dw_apb_empty_tty_send_does_not_claim_the_transmitter() {
        let uart = DwApbUart::new(ScriptedAccess::new([]));
        let _owner = uart.try_claim_tx().unwrap();

        assert_eq!(Uart::try_send_tty(&uart, b""), Ok(0));
    }

    #[ktest]
    fn dw_apb_tty_send_reports_errors_and_accepted_prefixes() {
        let uart = DwApbUart::new(ScriptedAccess::new([Err(())]));
        assert_eq!(Uart::try_send_tty(&uart, b"A"), Err(ConsoleSendError::Io));

        let access = ScriptedAccess::new([Ok(0), Ok(LSR_THRE)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);
        assert_eq!(Uart::try_send_tty(&uart, b"A"), Ok(1));
        assert_eq!(observation.read_count(), 2);

        let uart = DwApbUart::new(ScriptedAccess::new([Ok(LSR_THRE), Err(())]));
        assert_eq!(Uart::try_send_tty(&uart, b"AB"), Ok(1));
    }

    #[ktest]
    fn dw_apb_tty_send_does_not_expand_newlines() {
        let access = ScriptedAccess::new([Ok(LSR_THRE)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        assert_eq!(Uart::try_send_tty(&uart, b"\n"), Ok(1));
        assert_eq!(observation.writes(), vec![(THR_OFFSET, b'\n' as u32)]);
    }

    #[ktest]
    fn dw_apb_send_releases_ownership_after_failure() {
        let uart = DwApbUart::new(ScriptedAccess::new([Err(())]));

        assert_eq!(uart.try_send_with_budget(b"A", 1), Err(TxError::Mmio));
        assert!(!uart.tx_owned.load(Ordering::Relaxed));
    }

    #[ktest]
    fn dw_apb_receive_reads_shifted_status_and_data() {
        let access = ScriptedAccess::new([Ok(LSR_DR), Ok(0x141), Ok(LSR_DR), Ok(0x242)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);
        let mut input = [0; 2];

        assert_eq!(uart.receive_available(&mut input), 2);
        assert_eq!(input, [0x41, 0x42]);
        assert_eq!(
            observation.operations(),
            vec![
                Operation::Read32(LSR_OFFSET),
                Operation::Read32(RBR_OFFSET),
                Operation::Read32(LSR_OFFSET),
                Operation::Read32(RBR_OFFSET),
            ]
        );
    }

    #[ktest]
    fn dw_apb_receive_stops_on_empty_or_mmio_error() {
        let empty = ScriptedAccess::new([Ok(0)]);
        let uart = DwApbUart::new(empty);
        let mut unchanged = [0xff; 2];

        assert_eq!(uart.receive_available(&mut unchanged), 0);
        assert_eq!(unchanged, [0xff; 2]);

        let partial = ScriptedAccess::new([Ok(LSR_DR), Ok(0x41), Err(())]);
        let uart = DwApbUart::new(partial);
        let mut input = [0; 2];

        assert_eq!(uart.receive_available(&mut input), 1);
        assert_eq!(input, [0x41, 0]);
    }

    #[ktest]
    fn dw_apb_flush_has_one_fixed_receive_budget() {
        let access = ScriptedAccess::new([Ok(LSR_DR); RX_FLUSH_BUDGET * 2]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        Uart::flush(&uart);

        assert_eq!(observation.read_count(), RX_FLUSH_BUDGET * 2);
    }

    #[ktest]
    fn dw_apb_rx_enable_preserves_nonstandard_ier_bits() {
        let access = ScriptedAccess::new([Ok(0x80)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        let prepared = uart.prepare_rx_interrupt().unwrap();
        assert_eq!(
            observation.operations(),
            vec![Operation::Read32(IER_OFFSET)]
        );

        assert_eq!(uart.enable_rx_interrupt(prepared), Ok(()));
        assert_eq!(
            observation.operations(),
            vec![
                Operation::Read32(IER_OFFSET),
                Operation::Write32(IER_OFFSET, 0x81)
            ]
        );
    }

    #[ktest]
    fn dw_apb_rx_enable_rejects_unhandled_standard_sources() {
        let access = ScriptedAccess::new([Ok(0x02)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        assert_eq!(
            uart.prepare_rx_interrupt(),
            Err(RxEnableError::UnsupportedInterruptSources)
        );
        assert!(observation.writes().is_empty());
    }

    #[ktest]
    fn dw_apb_rx_enable_reports_mmio_errors() {
        let read_error = DwApbUart::new(ScriptedAccess::new([Err(())]));
        assert_eq!(read_error.prepare_rx_interrupt(), Err(RxEnableError::Mmio));

        let access = ScriptedAccess::with_write_results([Ok(0)], [Err(())]);
        let observation = access.clone();
        let write_error = DwApbUart::new(access);
        let prepared = write_error.prepare_rx_interrupt().unwrap();
        assert_eq!(
            write_error.enable_rx_interrupt(prepared),
            Err(RxEnableError::Mmio)
        );
        assert_eq!(observation.writes(), vec![(IER_OFFSET, IER_RDI)]);
    }

    #[ktest]
    fn dw_apb_rx_disable_preserves_nonstandard_ier_bits() {
        let access = ScriptedAccess::new([Ok(0x81)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        assert_eq!(uart.disable_rx_interrupt(), Ok(()));
        assert_eq!(
            observation.operations(),
            vec![
                Operation::Read32(IER_OFFSET),
                Operation::Write32(IER_OFFSET, 0x80),
            ]
        );
    }

    #[ktest]
    fn dw_apb_top_half_masks_receive_before_requesting_deferred_work() {
        let access = ScriptedAccess::new([Ok(IIR_RDI), Ok(0x81)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        assert_eq!(uart.prepare_deferred_rx(), Ok(true));
        assert_eq!(
            observation.operations(),
            vec![
                Operation::Read32(IIR_OFFSET),
                Operation::Read32(IER_OFFSET),
                Operation::Write32(IER_OFFSET, 0x80),
            ]
        );
    }

    #[ktest]
    fn dw_apb_clears_busy_detect_through_shifted_iir_and_usr() {
        let access = ScriptedAccess::new([Ok(IIR_BUSY), Ok(1)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        assert_eq!(
            uart.acknowledge_interrupt_cause(),
            Ok(RxInterruptCause::BusyCleared)
        );
        assert_eq!(
            observation.operations(),
            vec![Operation::Read32(IIR_OFFSET), Operation::Read32(USR_OFFSET),]
        );
    }

    #[ktest]
    fn dw_apb_quiesce_rejects_a_persistent_pending_cause_at_the_bound() {
        let access = ScriptedAccess::new([
            Ok(IIR_BUSY),
            Ok(1),
            Ok(IIR_BUSY),
            Ok(1),
            Ok(IIR_BUSY),
            Ok(1),
            Ok(IIR_BUSY),
            Ok(1),
        ]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        assert_eq!(
            uart.quiesce_rx_interrupt(),
            Err(RxServiceError::StillPending)
        );
        assert_eq!(observation.read_count(), RX_QUIESCE_CAUSE_BUDGET * 2);
    }

    #[ktest]
    fn dw_apb_uart_trait_delegates_send_receive_and_flush() {
        let access = ScriptedAccess::new([Ok(LSR_THRE), Ok(LSR_DR), Ok(0x42), Ok(0)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);
        let mut input = [0xff; 2];

        assert_eq!(Uart::try_send_diagnostic(&uart, b"A"), Ok(()));
        assert_eq!(observation.writes(), vec![(THR_OFFSET, 0x41)]);
        assert_eq!(Uart::recv(&uart, &mut input), 1);
        assert_eq!(input, [0x42, 0xff]);
        Uart::flush(&uart);
    }

    #[ktest]
    fn dw_apb_registration_probe_accepts_ready_hardware_without_writes() {
        let access = ScriptedAccess::new([Ok(LSR_THRE)]);
        let observation = access.clone();

        assert!(prepare_for_registration(access, 4).is_ok());
        assert_eq!(observation.read_count(), 1);
        assert!(observation.writes().is_empty());
    }

    #[ktest]
    fn dw_apb_registration_probe_rejects_stuck_hardware_at_the_bound() {
        let access = ScriptedAccess::new([Ok(0), Ok(0), Ok(0), Ok(0)]);
        let observation = access.clone();

        assert_eq!(
            prepare_for_registration(access, 4).err(),
            Some(TxError::TimedOut)
        );
        assert_eq!(observation.read_count(), 4);
        assert!(observation.writes().is_empty());
    }

    #[ktest]
    fn dw_apb_registration_probe_rejects_an_mmio_error_without_writes() {
        let access = ScriptedAccess::new([Err(())]);
        let observation = access.clone();

        assert_eq!(
            prepare_for_registration(access, 4).err(),
            Some(TxError::Mmio)
        );
        assert_eq!(observation.read_count(), 1);
        assert!(observation.writes().is_empty());
    }
}
