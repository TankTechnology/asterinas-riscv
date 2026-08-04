// SPDX-License-Identifier: MPL-2.0

//! SiFive UART transmit support for RISC-V device trees.
//!
//! The RISC-V UART dispatcher selects this driver for `sifive,uart0` nodes.

use alloc::{string::ToString, sync::Arc};
use core::{ops::Range, time::Duration};

use aster_console::{ConsoleSendError, ConsoleSendReadyError};
use fdt::node::FdtNode;
use ostd::{
    arch::{
        self,
        device::io_mem,
        irq::{self as arch_irq, MappedIrqLine},
    },
    io::IoMem,
    irq::IrqLine,
    mm::VmIoOnce,
    sync::{LocalIrqDisabled, Mutex, MutexGuard, SpinLock},
};
use spin::Once;

use super::ExplicitInterruptSourceError;
use crate::console::{self, DiagnosticSendError, Uart, UartConsole};

// The register layout, TXCNT field, and TXWM bit follow chapter 13 of the FU540 manual:
// https://starfivetech.com/uploads/fu540-c000-manual-v1p4.pdf.
// Initialization also follows the upstream U-Boot driver:
// https://github.com/u-boot/u-boot/blob/v2026.07/drivers/serial/serial_sifive.c.
const TXDATA_OFFSET_BYTES: usize = 0x00;
const TXCTRL_OFFSET_BYTES: usize = 0x08;
const INTERRUPT_ENABLE_OFFSET_BYTES: usize = 0x10;
const REQUIRED_MMIO_SIZE_BYTES: usize = INTERRUPT_ENABLE_OFFSET_BYTES + size_of::<u32>();
const TX_FIFO_FULL: u32 = 1 << 31;
const TX_ENABLE: u32 = 1;
const TX_WATERMARK_SHIFT: u32 = 16;
const TX_WATERMARK_LEVEL_BITS: u32 = 1 << TX_WATERMARK_SHIFT;
const TX_WATERMARK_INTERRUPT: u32 = 1;
const NANOSECONDS_PER_SECOND: u128 = 1_000_000_000;
// Firmware normally configures 115200 baud. One second also accommodates a
// much slower setup while bounding a stuck transmitter independently of CPU
// and MMIO speed.
const TX_INIT_READY_TIMEOUT: Duration = Duration::from_secs(1);

static TX_IRQ_LINE: Once<MappedIrqLine> = Once::new();

pub(super) fn init(fdt_node: FdtNode) {
    let config = match SiFiveMmioConfig::from_node(fdt_node) {
        Ok(config) => config,
        Err(error) => {
            ostd::warn!("failed to validate SiFive UART MMIO: {:?}", error);
            return;
        }
    };
    let Ok(io_mem) = IoMem::acquire(config.mmio_range()) else {
        ostd::error!("failed to acquire I/O memory for SiFive UART");
        return;
    };
    let uart = match prepare_uart(IoMemAccess { io_mem }) {
        Ok(uart) => uart,
        Err(error) => {
            ostd::error!("SiFive UART initialization failed: {:?}", error);
            return;
        }
    };

    let uart_console = UartConsole::new(uart);
    if let Err(error) = try_initialize_tx_interrupt(fdt_node, uart_console.clone()) {
        ostd::error!("SiFive UART interrupt initialization failed: {:?}", error);
        return;
    }
    aster_console::register_device(crate::CONSOLE_NAME.to_string(), uart_console);
    ostd::info!("Registered SiFive UART as a console");
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SiFiveMmioConfigError {
    Unavailable,
    InvalidStatus,
    MissingReg,
    MissingRegSize,
    MmioRangeTooSmall,
    MmioRangeOverflow,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct SiFiveMmioConfig {
    mmio_range: Range<usize>,
}

impl SiFiveMmioConfig {
    fn from_node(fdt_node: FdtNode) -> Result<Self, SiFiveMmioConfigError> {
        let status = match fdt_node.property("status") {
            Some(property) => Some(
                property
                    .as_str()
                    .ok_or(SiFiveMmioConfigError::InvalidStatus)?,
            ),
            None => None,
        };
        let reg = fdt_node
            .reg()
            .and_then(|mut regs| regs.next())
            .ok_or(SiFiveMmioConfigError::MissingReg)?;
        let reg_size_bytes = reg.size.ok_or(SiFiveMmioConfigError::MissingRegSize)?;

        Self::validate(status, reg.starting_address as usize, reg_size_bytes)
    }

    fn validate(
        status: Option<&str>,
        reg_start: usize,
        reg_size_bytes: usize,
    ) -> Result<Self, SiFiveMmioConfigError> {
        // Devicetree Specification, section 2.3.4, defines absent, "ok", and "okay"
        // as available.
        if !matches!(status, None | Some("ok" | "okay")) {
            return Err(SiFiveMmioConfigError::Unavailable);
        }
        if reg_size_bytes < REQUIRED_MMIO_SIZE_BYTES {
            return Err(SiFiveMmioConfigError::MmioRangeTooSmall);
        }
        let reg_end = reg_start
            .checked_add(reg_size_bytes)
            .ok_or(SiFiveMmioConfigError::MmioRangeOverflow)?;

        Ok(Self {
            mmio_range: reg_start..reg_end,
        })
    }

    fn mmio_range(&self) -> Range<usize> {
        self.mmio_range.clone()
    }
}

trait SiFiveAccess {
    // RISC-V atomics do not order the device I/O domain. Paired fences bridge
    // UART writes across the in-memory transmitter lock handoff.
    fn fence(&self);

    fn read32(&self, offset_bytes: usize) -> Result<u32, ()>;

    fn write32(&self, offset_bytes: usize, value: u32) -> Result<(), ()>;
}

struct IoMemAccess {
    io_mem: IoMem,
}

impl SiFiveAccess for IoMemAccess {
    fn fence(&self) {
        io_mem::fence();
    }

    fn read32(&self, offset_bytes: usize) -> Result<u32, ()> {
        self.io_mem.read_once::<u32>(offset_bytes).map_err(|_| ())
    }

    fn write32(&self, offset_bytes: usize, value: u32) -> Result<(), ()> {
        self.io_mem.write_once(offset_bytes, &value).map_err(|_| ())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ImmediateTxError {
    Busy,
    Mmio,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TxInterruptError {
    Mmio,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TxInitError {
    Mmio,
    TimedOut,
}

struct TxOwnerGuard<'a, A: SiFiveAccess> {
    uart: &'a SiFiveUart<A>,
    _lock_guard: MutexGuard<'a, ()>,
}

impl<A: SiFiveAccess> Drop for TxOwnerGuard<'_, A> {
    fn drop(&mut self) {
        self.uart.access.fence();
    }
}

impl<A: SiFiveAccess> TxOwnerGuard<'_, A> {
    fn send_byte_immediate(&self, byte: u8) -> Result<(), ImmediateTxError> {
        check_tx_ready_once(&self.uart.access)?;
        self.uart
            .access
            .write32(TXDATA_OFFSET_BYTES, u32::from(byte))
            .map_err(|_| ImmediateTxError::Mmio)
    }

    fn poll_send_ready(self) -> Result<bool, ImmediateTxError> {
        let uart = self.uart;

        match check_tx_ready_once(&uart.access) {
            Ok(()) => return Ok(true),
            Err(ImmediateTxError::Busy) => {}
            Err(error) => return Err(error),
        }

        // The IRQ handler may notify another hart while the source is armed.
        // Release TX ownership first so the woken writer can make progress.
        drop(self);
        uart.arm_tx_interrupt_and_recheck()
    }
}

struct TxInterruptState {
    desired_value: SpinLock<u32, LocalIrqDisabled>,
}

impl TxInterruptState {
    fn new() -> Self {
        Self {
            desired_value: SpinLock::new(0),
        }
    }

    fn enable<A: SiFiveAccess>(&self, access: &A) -> Result<(), TxInterruptError> {
        self.set_desired_value(access, TX_WATERMARK_INTERRUPT)
    }

    fn disable<A: SiFiveAccess>(&self, access: &A) -> Result<(), TxInterruptError> {
        self.set_desired_value(access, 0)
    }

    fn set_desired_value<A: SiFiveAccess>(
        &self,
        access: &A,
        desired_value: u32,
    ) -> Result<(), TxInterruptError> {
        *self.desired_value.lock() = desired_value;

        loop {
            let desired_value = *self.desired_value.lock();

            // The state lock disables local IRQs, so it cannot span potentially
            // stalled MMIO. A fenced write is complete only if the desired
            // value is still current; otherwise, repeat with the latest value.
            access.fence();
            access
                .write32(INTERRUPT_ENABLE_OFFSET_BYTES, desired_value)
                .map_err(|_| TxInterruptError::Mmio)?;
            access.fence();

            if *self.desired_value.lock() == desired_value {
                return Ok(());
            }
        }
    }
}

struct SiFiveUart<A> {
    access: A,
    tx_lock: Mutex<()>,
    tx_interrupt_state: TxInterruptState,
}

impl<A: SiFiveAccess> SiFiveUart<A> {
    fn new(access: A) -> Self {
        Self {
            access,
            tx_lock: Mutex::new(()),
            tx_interrupt_state: TxInterruptState::new(),
        }
    }

    fn try_send_diagnostic_crlf_immediate(&self, buf: &[u8]) -> Result<(), ImmediateTxError> {
        if buf.is_empty() {
            return Ok(());
        }
        let owner = self.try_claim_tx()?;

        for &byte in buf {
            if byte == b'\n' {
                owner.send_byte_immediate(b'\r')?;
            }
            owner.send_byte_immediate(byte)?;
        }

        Ok(())
    }

    fn try_send_tty_immediate(&self, buf: &[u8]) -> Result<usize, ImmediateTxError> {
        if buf.is_empty() {
            return Ok(0);
        }
        let owner = self.try_claim_tx()?;
        for (index, &byte) in buf.iter().enumerate() {
            if let Err(error) = owner.send_byte_immediate(byte) {
                return if index == 0 { Err(error) } else { Ok(index) };
            }
        }

        Ok(buf.len())
    }

    // Match the DW APB console: nested or concurrent output returns promptly
    // instead of waiting while the owner performs MMIO. Ownership still lasts
    // for one send call so buffers and translated newlines do not interleave.
    fn try_claim_tx(&self) -> Result<TxOwnerGuard<'_, A>, ImmediateTxError> {
        let lock_guard = self.tx_lock.try_lock().ok_or(ImmediateTxError::Busy)?;
        self.access.fence();

        Ok(TxOwnerGuard {
            uart: self,
            _lock_guard: lock_guard,
        })
    }

    fn arm_tx_interrupt_and_recheck(&self) -> Result<bool, ImmediateTxError> {
        self.tx_interrupt_state
            .enable(&self.access)
            .map_err(|_| ImmediateTxError::Mmio)?;

        match check_tx_ready_once(&self.access) {
            Err(ImmediateTxError::Busy) => return Ok(false),
            Err(error) => return Err(error),
            Ok(()) => {}
        }

        self.disable_tx_interrupt()
            .map_err(|_| ImmediateTxError::Mmio)?;
        Ok(true)
    }

    fn disable_tx_interrupt(&self) -> Result<(), TxInterruptError> {
        self.tx_interrupt_state.disable(&self.access)
    }
}

impl<A: SiFiveAccess> Uart for SiFiveUart<A> {
    fn try_send_diagnostic(&self, buf: &[u8]) -> Result<(), DiagnosticSendError> {
        match self.try_send_diagnostic_crlf_immediate(buf) {
            Ok(()) | Err(ImmediateTxError::Busy) => Ok(()),
            Err(ImmediateTxError::Mmio) => Err(DiagnosticSendError::Io),
        }
    }

    fn poll_send_ready(&self) -> Result<bool, ConsoleSendReadyError> {
        let Ok(owner) = self.try_claim_tx() else {
            return Ok(false);
        };
        owner
            .poll_send_ready()
            .map_err(|_error| ConsoleSendReadyError::Io)
    }

    fn try_send_tty(&self, buf: &[u8]) -> Result<usize, ConsoleSendError> {
        self.try_send_tty_immediate(buf)
            .map_err(|error| match error {
                ImmediateTxError::Busy => ConsoleSendError::Busy,
                ImmediateTxError::Mmio => ConsoleSendError::Io,
            })
    }

    fn recv(&self, _buf: &mut [u8]) -> usize {
        0
    }

    fn flush(&self) {}
}

struct Deadline {
    start_ticks: u64,
    timeout_ticks: u64,
}

impl Deadline {
    fn after(duration: Duration) -> Self {
        Self {
            start_ticks: arch::read_tsc(),
            timeout_ticks: duration_to_ticks(duration, arch::tsc_freq()),
        }
    }

    fn expired(&self) -> bool {
        arch::read_tsc().wrapping_sub(self.start_ticks) >= self.timeout_ticks
    }
}

fn duration_to_ticks(duration: Duration, ticks_per_second: u64) -> u64 {
    duration
        .as_nanos()
        .saturating_mul(ticks_per_second as u128)
        .div_ceil(NANOSECONDS_PER_SECOND)
        .min(u64::MAX as u128) as u64
}

fn wait_for_initial_tx_ready<A: SiFiveAccess>(access: &A) -> Result<(), TxInitError> {
    let deadline = Deadline::after(TX_INIT_READY_TIMEOUT);
    wait_for_initial_tx_until(access, || deadline.expired())
}

fn check_tx_ready_once<A: SiFiveAccess>(access: &A) -> Result<(), ImmediateTxError> {
    let state = access
        .read32(TXDATA_OFFSET_BYTES)
        .map_err(|_| ImmediateTxError::Mmio)?;
    if state & TX_FIFO_FULL == 0 {
        Ok(())
    } else {
        Err(ImmediateTxError::Busy)
    }
}

fn wait_for_initial_tx_until<A: SiFiveAccess>(
    access: &A,
    mut deadline_expired_fn: impl FnMut() -> bool,
) -> Result<(), TxInitError> {
    loop {
        let state = access
            .read32(TXDATA_OFFSET_BYTES)
            .map_err(|_| TxInitError::Mmio)?;
        if state & TX_FIFO_FULL == 0 {
            return Ok(());
        }
        if deadline_expired_fn() {
            return Err(TxInitError::TimedOut);
        }
    }
}

#[cfg(ktest)]
fn wait_for_initial_tx_with_budget<A: SiFiveAccess>(
    access: &A,
    mut remaining_polls: usize,
) -> Result<(), TxInitError> {
    wait_for_initial_tx_until(access, || {
        remaining_polls = remaining_polls.saturating_sub(1);
        remaining_polls == 0
    })
}

fn prepare_uart<A: SiFiveAccess>(access: A) -> Result<SiFiveUart<A>, TxInitError> {
    prepare_uart_with(access, wait_for_initial_tx_ready)
}

fn prepare_uart_with<A: SiFiveAccess>(
    access: A,
    mut wait_fn: impl FnMut(&A) -> Result<(), TxInitError>,
) -> Result<SiFiveUart<A>, TxInitError> {
    access
        .write32(INTERRUPT_ENABLE_OFFSET_BYTES, 0)
        .map_err(|_| TxInitError::Mmio)?;
    access
        .write32(TXCTRL_OFFSET_BYTES, TX_ENABLE | TX_WATERMARK_LEVEL_BITS)
        .map_err(|_| TxInitError::Mmio)?;
    access.fence();
    wait_fn(&access)?;
    Ok(SiFiveUart::new(access))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TxIrqInitError {
    InvalidConfig(ExplicitInterruptSourceError),
    IrqLineUnavailable,
    IrqChipUnavailable,
    MapFailed,
}

fn try_initialize_tx_interrupt(
    fdt_node: FdtNode,
    uart_console: Arc<UartConsole<SiFiveUart<IoMemAccess>>>,
) -> Result<(), TxIrqInitError> {
    let interrupt_source =
        super::parse_explicit_interrupt_source(fdt_node).map_err(TxIrqInitError::InvalidConfig)?;
    let mut irq_line = IrqLine::alloc().map_err(|_| TxIrqInitError::IrqLineUnavailable)?;

    let callback_console = uart_console.clone();
    irq_line.on_active(
        move |_| match callback_console.uart().disable_tx_interrupt() {
            Ok(()) => callback_console.notify_send_ready(),
            Err(TxInterruptError::Mmio) => {
                console::handle_fatal_diagnostic_error(DiagnosticSendError::Io)
            }
        },
    );

    let irq_chip = arch_irq::IRQ_CHIP
        .get()
        .ok_or(TxIrqInitError::IrqChipUnavailable)?;
    let mapped_irq_line = irq_chip
        .map_fdt_pin_to(interrupt_source, irq_line)
        .map_err(|_| TxIrqInitError::MapFailed)?;
    TX_IRQ_LINE.call_once(move || mapped_irq_line);

    Ok(())
}

#[cfg(ktest)]
mod tests {
    use alloc::{collections::VecDeque, vec, vec::Vec};
    use core::sync::atomic::{AtomicBool, Ordering};

    use ostd::prelude::*;
    use spin::Mutex;

    use super::*;

    #[derive(Clone, Debug, Eq, PartialEq)]
    enum Operation {
        Fence,
        Read32(usize),
        Write32(usize, u32),
    }

    struct State {
        reads: VecDeque<Result<u32, ()>>,
        write_results: VecDeque<Result<(), ()>>,
        operations: Vec<Operation>,
    }

    #[derive(Clone)]
    struct ScriptedAccess(Arc<Mutex<State>>);

    impl ScriptedAccess {
        fn new<const N: usize>(reads: [Result<u32, ()>; N]) -> Self {
            Self(Arc::new(Mutex::new(State {
                reads: reads.into_iter().collect(),
                write_results: VecDeque::new(),
                operations: Vec::new(),
            })))
        }

        fn with_write_results<const N: usize, const M: usize>(
            reads: [Result<u32, ()>; N],
            write_results: [Result<(), ()>; M],
        ) -> Self {
            Self(Arc::new(Mutex::new(State {
                reads: reads.into_iter().collect(),
                write_results: write_results.into_iter().collect(),
                operations: Vec::new(),
            })))
        }

        fn writes(&self) -> Vec<(usize, u32)> {
            self.0
                .lock()
                .operations
                .iter()
                .filter_map(|operation| match operation {
                    Operation::Write32(offset, value) => Some((*offset, *value)),
                    _ => None,
                })
                .collect()
        }

        fn fence_count(&self) -> usize {
            self.0
                .lock()
                .operations
                .iter()
                .filter(|operation| matches!(operation, Operation::Fence))
                .count()
        }

        fn operations(&self) -> Vec<Operation> {
            self.0.lock().operations.clone()
        }
    }

    impl SiFiveAccess for ScriptedAccess {
        fn fence(&self) {
            self.0.lock().operations.push(Operation::Fence);
        }

        fn read32(&self, offset_bytes: usize) -> Result<u32, ()> {
            let mut state = self.0.lock();
            state.operations.push(Operation::Read32(offset_bytes));
            state.reads.pop_front().unwrap()
        }

        fn write32(&self, offset_bytes: usize, value: u32) -> Result<(), ()> {
            let mut state = self.0.lock();
            state
                .operations
                .push(Operation::Write32(offset_bytes, value));
            state.write_results.pop_front().unwrap_or(Ok(()))
        }
    }

    struct InterleavingAccess {
        tx_interrupt_state: Arc<TxInterruptState>,
        should_interleave_disable: AtomicBool,
        writes: Mutex<Vec<u32>>,
    }

    impl SiFiveAccess for InterleavingAccess {
        fn fence(&self) {}

        fn read32(&self, _offset_bytes: usize) -> Result<u32, ()> {
            unreachable!()
        }

        fn write32(&self, offset_bytes: usize, value: u32) -> Result<(), ()> {
            assert_eq!(offset_bytes, INTERRUPT_ENABLE_OFFSET_BYTES);
            assert!(self.tx_interrupt_state.desired_value.try_lock().is_some());
            self.writes.lock().push(value);

            if value == TX_WATERMARK_INTERRUPT
                && self
                    .should_interleave_disable
                    .swap(false, Ordering::Relaxed)
            {
                self.tx_interrupt_state.disable(self).unwrap();
            }

            Ok(())
        }
    }

    #[ktest]
    fn tx_holds_one_ownership_across_the_buffer() {
        let access = ScriptedAccess::new([Ok(0), Ok(0)]);
        let uart = SiFiveUart::new(access.clone());

        assert_eq!(uart.try_send_diagnostic_crlf_immediate(b"xy"), Ok(()));
        assert_eq!(access.fence_count(), 2);
        assert_eq!(
            access.operations(),
            vec![
                Operation::Fence,
                Operation::Read32(TXDATA_OFFSET_BYTES),
                Operation::Write32(TXDATA_OFFSET_BYTES, b'x' as u32),
                Operation::Read32(TXDATA_OFFSET_BYTES),
                Operation::Write32(TXDATA_OFFSET_BYTES, b'y' as u32),
                Operation::Fence,
            ]
        );
    }

    #[ktest]
    fn diagnostic_immediate_path_inserts_carriage_return() {
        let access = ScriptedAccess::new([Ok(0), Ok(0)]);
        let uart = SiFiveUart::new(access.clone());

        assert_eq!(uart.try_send_diagnostic_crlf_immediate(b"\n"), Ok(()));
        assert_eq!(
            access.writes(),
            vec![
                (TXDATA_OFFSET_BYTES, b'\r' as u32),
                (TXDATA_OFFSET_BYTES, b'\n' as u32)
            ]
        );
    }

    #[ktest]
    fn initial_tx_timeout_does_not_write() {
        let access = ScriptedAccess::new([Ok(TX_FIFO_FULL), Ok(TX_FIFO_FULL)]);

        assert_eq!(
            wait_for_initial_tx_with_budget(&access, 2),
            Err(TxInitError::TimedOut)
        );
        assert!(access.writes().is_empty());
    }

    #[ktest]
    fn ready_fifo_wins_when_the_deadline_has_expired() {
        let access = ScriptedAccess::new([Ok(0)]);

        assert_eq!(wait_for_initial_tx_until(&access, || true), Ok(()));
        assert_eq!(
            access.operations(),
            vec![Operation::Read32(TXDATA_OFFSET_BYTES)]
        );
    }

    #[ktest]
    fn tx_contention_is_nonblocking() {
        let access = ScriptedAccess::new([Ok(0)]);
        let uart = SiFiveUart::new(access.clone());
        let owner = uart.try_claim_tx().unwrap();

        assert_eq!(
            uart.try_send_diagnostic_crlf_immediate(b"x"),
            Err(ImmediateTxError::Busy)
        );
        assert_eq!(uart.poll_send_ready(), Ok(false));
        assert_eq!(access.fence_count(), 1);
        assert!(access.writes().is_empty());

        drop(owner);
        assert_eq!(uart.poll_send_ready(), Ok(true));
    }

    #[ktest]
    fn full_fifo_arms_transmit_watermark_interrupt() {
        let access = ScriptedAccess::new([Ok(TX_FIFO_FULL), Ok(TX_FIFO_FULL)]);
        let uart = SiFiveUart::new(access.clone());

        assert_eq!(uart.poll_send_ready(), Ok(false));
        assert_eq!(
            access.writes(),
            vec![(INTERRUPT_ENABLE_OFFSET_BYTES, TX_WATERMARK_INTERRUPT)]
        );
    }

    #[ktest]
    fn readiness_reports_interrupt_enable_mmio_failure() {
        let access = ScriptedAccess::with_write_results([Ok(TX_FIFO_FULL)], [Err(())]);
        let uart = SiFiveUart::new(access.clone());

        assert_eq!(uart.poll_send_ready(), Err(ConsoleSendReadyError::Io));
        assert_eq!(
            access.writes(),
            vec![(INTERRUPT_ENABLE_OFFSET_BYTES, TX_WATERMARK_INTERRUPT)]
        );
    }

    #[ktest]
    fn ready_fifo_does_not_arm_transmit_interrupt() {
        let access = ScriptedAccess::new([Ok(0)]);
        let uart = SiFiveUart::new(access.clone());

        assert_eq!(uart.poll_send_ready(), Ok(true));
        assert!(access.writes().is_empty());
        assert_eq!(
            access.operations(),
            vec![
                Operation::Fence,
                Operation::Read32(TXDATA_OFFSET_BYTES),
                Operation::Fence,
            ]
        );
    }

    #[ktest]
    fn readiness_recheck_disarms_an_interrupt_after_fifo_progress() {
        let access = ScriptedAccess::new([Ok(TX_FIFO_FULL), Ok(0)]);
        let uart = SiFiveUart::new(access.clone());

        assert_eq!(uart.poll_send_ready(), Ok(true));
        assert_eq!(
            access.writes(),
            vec![
                (INTERRUPT_ENABLE_OFFSET_BYTES, TX_WATERMARK_INTERRUPT),
                (INTERRUPT_ENABLE_OFFSET_BYTES, 0),
            ]
        );
    }

    #[ktest]
    fn tx_immediate_path_never_polls_a_full_fifo() {
        let access = ScriptedAccess::new([Ok(TX_FIFO_FULL)]);
        let uart = SiFiveUart::new(access.clone());

        assert_eq!(
            uart.try_send_diagnostic_crlf_immediate(b"x"),
            Err(ImmediateTxError::Busy)
        );
        assert_eq!(
            access.operations(),
            vec![
                Operation::Fence,
                Operation::Read32(TXDATA_OFFSET_BYTES),
                Operation::Fence,
            ]
        );
    }

    #[ktest]
    fn tty_immediate_path_preserves_newlines() {
        let access = ScriptedAccess::new([Ok(0)]);
        let uart = SiFiveUart::new(access.clone());

        assert_eq!(uart.try_send_tty_immediate(b"\n"), Ok(1));
        assert_eq!(access.writes(), vec![(TXDATA_OFFSET_BYTES, b'\n' as u32)]);
    }

    #[ktest]
    fn tty_mmio_failure_becomes_a_console_io_error() {
        let uart = SiFiveUart::new(ScriptedAccess::new([Err(())]));

        assert_eq!(Uart::try_send_tty(&uart, b"x"), Err(ConsoleSendError::Io));
    }

    #[ktest]
    fn diagnostic_mmio_failure_reaches_the_shared_policy_boundary() {
        let uart = SiFiveUart::new(ScriptedAccess::new([Err(())]));

        assert_eq!(
            Uart::try_send_diagnostic(&uart, b"x"),
            Err(DiagnosticSendError::Io)
        );
    }

    #[ktest]
    fn diagnostic_contention_remains_best_effort() {
        let access = ScriptedAccess::new([Ok(0)]);
        let uart = SiFiveUart::new(access);
        let owner = uart.try_claim_tx().unwrap();

        assert_eq!(Uart::try_send_diagnostic(&uart, b"x"), Ok(()));
        drop(owner);
    }

    #[ktest]
    fn preparation_enables_transmit_before_waiting_for_space() {
        let access = ScriptedAccess::new([Ok(0)]);

        assert!(
            prepare_uart_with(access.clone(), |access| {
                wait_for_initial_tx_with_budget(access, 1)
            })
            .is_ok()
        );
        assert_eq!(
            access.operations(),
            vec![
                Operation::Write32(INTERRUPT_ENABLE_OFFSET_BYTES, 0),
                Operation::Write32(TXCTRL_OFFSET_BYTES, TX_ENABLE | TX_WATERMARK_LEVEL_BITS),
                Operation::Fence,
                Operation::Read32(TXDATA_OFFSET_BYTES),
            ]
        );
    }

    #[ktest]
    fn timeout_ticks_round_up() {
        assert_eq!(duration_to_ticks(Duration::from_nanos(1), 10), 1);
        assert_eq!(duration_to_ticks(Duration::from_secs(2), 10), 20);
    }

    #[ktest]
    fn mmio_config_accepts_only_available_device_tree_nodes() {
        for status in [None, Some("ok"), Some("okay")] {
            assert!(SiFiveMmioConfig::validate(status, 0x1000, REQUIRED_MMIO_SIZE_BYTES).is_ok());
        }
        for status in [Some("disabled"), Some("reserved"), Some("fail")] {
            assert_eq!(
                SiFiveMmioConfig::validate(status, 0x1000, REQUIRED_MMIO_SIZE_BYTES),
                Err(SiFiveMmioConfigError::Unavailable)
            );
        }
    }

    #[ktest]
    fn mmio_config_rejects_invalid_ranges() {
        assert_eq!(
            SiFiveMmioConfig::validate(None, 0x1000, REQUIRED_MMIO_SIZE_BYTES - 1),
            Err(SiFiveMmioConfigError::MmioRangeTooSmall)
        );
        assert_eq!(
            SiFiveMmioConfig::validate(None, usize::MAX, REQUIRED_MMIO_SIZE_BYTES),
            Err(SiFiveMmioConfigError::MmioRangeOverflow)
        );
    }

    #[ktest]
    fn disable_tx_interrupt_clears_the_enable_register() {
        let access = ScriptedAccess::new([]);
        let uart = SiFiveUart::new(access.clone());

        assert_eq!(uart.disable_tx_interrupt(), Ok(()));
        assert_eq!(access.writes(), vec![(INTERRUPT_ENABLE_OFFSET_BYTES, 0)]);
        assert_eq!(access.fence_count(), 2);
    }

    #[ktest]
    fn tx_interrupt_can_be_disabled_while_the_transmitter_is_owned() {
        let access = ScriptedAccess::new([]);
        let uart = SiFiveUart::new(access.clone());
        let _owner = uart.try_claim_tx().unwrap();

        assert_eq!(uart.disable_tx_interrupt(), Ok(()));
        assert_eq!(access.writes(), vec![(INTERRUPT_ENABLE_OFFSET_BYTES, 0)]);
    }

    #[ktest]
    fn poll_send_ready_releases_transmitter_ownership() {
        let access = ScriptedAccess::new([Ok(TX_FIFO_FULL), Ok(TX_FIFO_FULL)]);
        let uart = SiFiveUart::new(access.clone());
        let owner = uart.try_claim_tx().unwrap();

        assert_eq!(owner.poll_send_ready(), Ok(false));
        assert!(uart.try_claim_tx().is_ok());
        assert_eq!(
            access.writes(),
            vec![(INTERRUPT_ENABLE_OFFSET_BYTES, TX_WATERMARK_INTERRUPT)]
        );
    }

    #[ktest]
    fn concurrent_disable_repairs_an_outdated_interrupt_enable_write() {
        let tx_interrupt_state = Arc::new(TxInterruptState::new());
        let access = InterleavingAccess {
            tx_interrupt_state: tx_interrupt_state.clone(),
            should_interleave_disable: AtomicBool::new(true),
            writes: Mutex::new(Vec::new()),
        };

        assert_eq!(tx_interrupt_state.enable(&access), Ok(()));
        assert_eq!(*access.writes.lock(), vec![TX_WATERMARK_INTERRUPT, 0, 0]);
        assert_eq!(*tx_interrupt_state.desired_value.lock(), 0);
    }
}
