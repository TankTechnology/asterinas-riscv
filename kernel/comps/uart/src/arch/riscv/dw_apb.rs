// SPDX-License-Identifier: MPL-2.0

//! Firmware-preserving DesignWare APB UART support for RISC-V.
//!
//! This module validates the firmware-selected device,
//! probes its line status, and connects transmit-only access to [`UartConsole`].
//! It deliberately leaves baud rate, line control, FIFO state, and interrupts unchanged.
//! Runtime MMIO invariant failures are reported through the SBI-backed early console,
//! followed by a stack trace and system abort.

use alloc::string::ToString;
use core::{
    hint,
    ops::Range,
    sync::atomic::{AtomicU8, Ordering},
    time::Duration,
};

use fdt::node::FdtNode;
use ostd::{
    arch::{self, device::io_mem},
    io::IoMem,
    irq,
    mm::VmIoOnce,
    sync::{LocalIrqDisabled, SpinLock, SpinLockGuard},
};

use crate::console::{Uart, UartConsole};

// The layout properties follow the Devicetree binding:
// https://www.kernel.org/doc/Documentation/devicetree/bindings/serial/snps-dw-apb-uart.yaml
const REGISTER_SHIFT: usize = 2;
const REGISTER_IO_WIDTH_BYTES: usize = size_of::<u32>();
// The register indexes and THRE bit follow Linux's 16550 definitions:
// https://github.com/torvalds/linux/blob/master/include/uapi/linux/serial_reg.h
const TRANSMIT_HOLDING_OFFSET_BYTES: usize = 0;
const INTERRUPT_ENABLE_OFFSET_BYTES: usize = 1 << REGISTER_SHIFT;
const LINE_CONTROL_OFFSET_BYTES: usize = 3 << REGISTER_SHIFT;
const LINE_STATUS_OFFSET_BYTES: usize = 5 << REGISTER_SHIFT;
const REQUIRED_MMIO_SIZE_BYTES: usize = LINE_STATUS_OFFSET_BYTES + REGISTER_IO_WIDTH_BYTES;
const IER_PTIME: u32 = 1 << 7;
const LCR_DLAB: u32 = 1 << 7;
const LSR_THRE: u32 = 1 << 5;
// One second covers a character at every standard nonzero termios baud rate.
// The deadline prevents a broken device from stalling the system indefinitely.
const TX_TIMEOUT: Duration = Duration::from_secs(1);
const TX_HEALTHY: u8 = 0;
const TX_REPORTING: u8 = 1;
const TX_REPORTED: u8 = 2;

pub(super) fn init(fdt_node: FdtNode) {
    let config = match DwApbConfig::from_node(fdt_node) {
        Ok(config) => config,
        Err(error) => {
            ostd::warn!("failed to validate DW APB UART: {:?}", error);
            return;
        }
    };
    let Ok(io_mem) = IoMem::acquire(config.mmio_range()) else {
        ostd::error!("failed to acquire I/O memory for DW APB UART");
        return;
    };
    let uart = match prepare_for_registration(IoMemAccess { io_mem }, TX_TIMEOUT) {
        Ok(uart) => uart,
        // The firmware debug console may use the same UART. Avoid reporting a
        // stuck transmitter through that potentially blocked path.
        Err(RegistrationError::Tx(TxError::TimedOut)) => return,
        Err(error) => {
            ostd::error!("failed to probe DW APB UART readiness: {:?}", error);
            return;
        }
    };

    let uart_console = UartConsole::new(uart);
    aster_console::register_device(crate::CONSOLE_NAME.to_string(), uart_console);
    ostd::info!("registered DW APB UART as a console");
}

trait DwApbAccess {
    // RISC-V atomic ordering covers memory, but not the MMIO I/O domain.
    // The paired fences bridge both domains across transmitter ownership handoff.
    fn fence(&self);

    fn read32(&self, offset_bytes: usize) -> Result<u32, ()>;

    fn write32(&self, offset_bytes: usize, value: u32) -> Result<(), ()>;
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TxError {
    Unavailable,
    Mmio,
    TimedOut,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RegistrationError {
    Tx(TxError),
    UnsupportedConfiguration,
}

impl From<TxError> for RegistrationError {
    fn from(error: TxError) -> Self {
        Self::Tx(error)
    }
}

struct DwApbUart<A> {
    access: A,
    tx_lock: SpinLock<(), LocalIrqDisabled>,
    // The lock owner publishes REPORTING before releasing ownership. Other
    // harts then wait until its fallback diagnostic is complete, avoiding
    // duplicate or interleaved fatal reports.
    failure_state: AtomicU8,
}

struct TxOwnerGuard<'a, A: DwApbAccess> {
    uart: &'a DwApbUart<A>,
    _lock: SpinLockGuard<'a, (), LocalIrqDisabled>,
}

impl<A: DwApbAccess> Drop for TxOwnerGuard<'_, A> {
    fn drop(&mut self) {
        self.uart.access.fence();
    }
}

impl<A: DwApbAccess> DwApbUart<A> {
    fn new(access: A) -> Self {
        Self {
            access,
            tx_lock: SpinLock::new(()),
            failure_state: AtomicU8::new(TX_HEALTHY),
        }
    }

    fn try_send(&self, buf: &[u8]) -> Result<(), TxError> {
        self.try_send_with_timeout(buf, TX_TIMEOUT)
    }

    fn try_send_with_timeout(&self, buf: &[u8], timeout: Duration) -> Result<(), TxError> {
        if self.failure_state.load(Ordering::Acquire) != TX_HEALTHY {
            return Err(TxError::Unavailable);
        }

        let _owner = self.claim_tx()?;
        for &byte in buf {
            if byte == b'\n' {
                self.send_byte(b'\r', timeout)?;
            }
            self.send_byte(byte, timeout)?;
        }

        Ok(())
    }

    fn claim_tx(&self) -> Result<TxOwnerGuard<'_, A>, TxError> {
        self.finish_claim(self.tx_lock.lock())
    }

    fn finish_claim<'a>(
        &'a self,
        lock: SpinLockGuard<'a, (), LocalIrqDisabled>,
    ) -> Result<TxOwnerGuard<'a, A>, TxError> {
        self.access.fence();
        let owner = TxOwnerGuard {
            uart: self,
            _lock: lock,
        };
        if self.failure_state.load(Ordering::Acquire) != TX_HEALTHY {
            return Err(TxError::Unavailable);
        }
        Ok(owner)
    }

    fn send_byte(&self, byte: u8, timeout: Duration) -> Result<(), TxError> {
        if let Err(error) = wait_ready(&self.access, timeout) {
            self.failure_state.store(TX_REPORTING, Ordering::Release);
            return Err(error);
        }
        if self
            .access
            .write32(TRANSMIT_HOLDING_OFFSET_BYTES, u32::from(byte))
            .is_err()
        {
            self.failure_state.store(TX_REPORTING, Ordering::Release);
            return Err(TxError::Mmio);
        }
        Ok(())
    }

    fn mark_failure_reported(&self) {
        self.failure_state.store(TX_REPORTED, Ordering::Release);
    }

    fn wait_for_failure_report(&self) {
        while self.failure_state.load(Ordering::Acquire) == TX_REPORTING {
            hint::spin_loop();
        }
    }
}

impl<A: DwApbAccess + Send + Sync> Uart for DwApbUart<A> {
    fn send(&self, buf: &[u8]) {
        let _irq_guard = irq::disable_local();

        match self.try_send(buf) {
            Ok(()) => {}
            Err(TxError::Mmio) => {
                ostd::early_println!("fatal DW APB UART MMIO failure");
                ostd::panic::print_stack_trace();
                self.mark_failure_reported();
                ostd::panic::abort();
            }
            // The SBI debug console may use the same stuck UART, so do not try
            // to report a readiness timeout through it.
            Err(TxError::TimedOut) => {
                self.mark_failure_reported();
                ostd::panic::abort();
            }
            Err(TxError::Unavailable) => {
                self.wait_for_failure_report();
                ostd::panic::abort();
            }
        }
    }

    fn recv(&self, _buf: &mut [u8]) -> usize {
        0
    }

    fn flush(&self) {}
}

fn is_ready<A: DwApbAccess>(access: &A) -> Result<bool, TxError> {
    access
        .read32(LINE_STATUS_OFFSET_BYTES)
        .map(|line_status| line_status & LSR_THRE != 0)
        .map_err(|_| TxError::Mmio)
}

fn wait_ready<A: DwApbAccess>(access: &A, timeout: Duration) -> Result<(), TxError> {
    let start_ticks = arch::read_tsc();
    let timeout_ticks = duration_to_ticks(timeout);

    loop {
        if is_ready(access)? {
            return Ok(());
        }
        if has_timed_out(start_ticks, timeout_ticks) {
            return Err(TxError::TimedOut);
        }
        hint::spin_loop();
    }
}

fn duration_to_ticks(duration: Duration) -> u64 {
    const NANOS_PER_SECOND: u128 = 1_000_000_000;

    let scaled_ticks = duration
        .as_nanos()
        .saturating_mul(u128::from(arch::tsc_freq()));
    let rounded_ticks =
        scaled_ticks / NANOS_PER_SECOND + u128::from(scaled_ticks % NANOS_PER_SECOND != 0);
    rounded_ticks.min(u128::from(u64::MAX)) as u64
}

fn has_timed_out(start_ticks: u64, timeout_ticks: u64) -> bool {
    arch::read_tsc().wrapping_sub(start_ticks) >= timeout_ticks
}

fn probe_ready<A: DwApbAccess>(access: &A, timeout: Duration) -> Result<(), TxError> {
    wait_ready(access, timeout)
}

fn validate_firmware_state<A: DwApbAccess>(access: &A) -> Result<(), RegistrationError> {
    let line_control = access
        .read32(LINE_CONTROL_OFFSET_BYTES)
        .map_err(|_| TxError::Mmio)?;
    if line_control & LCR_DLAB != 0 {
        return Err(RegistrationError::UnsupportedConfiguration);
    }

    let interrupt_enable = access
        .read32(INTERRUPT_ENABLE_OFFSET_BYTES)
        .map_err(|_| TxError::Mmio)?;
    if interrupt_enable & IER_PTIME != 0 {
        return Err(RegistrationError::UnsupportedConfiguration);
    }

    Ok(())
}

fn prepare_for_registration<A: DwApbAccess>(
    access: A,
    timeout: Duration,
) -> Result<DwApbUart<A>, RegistrationError> {
    validate_firmware_state(&access)?;
    probe_ready(&access, timeout)?;
    Ok(DwApbUart::new(access))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DwApbConfigError {
    Disabled,
    InvalidStatus,
    MissingReg,
    MissingRegSize,
    MissingRegShift,
    InvalidRegShift,
    UnsupportedRegShift,
    MissingRegIoWidth,
    InvalidRegIoWidth,
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
        let reg = fdt_node
            .reg()
            .and_then(|mut regions| regions.next())
            .ok_or(DwApbConfigError::MissingReg)?;
        let reg_size_bytes = reg.size.ok_or(DwApbConfigError::MissingRegSize)?;
        let reg_shift = fdt_node
            .property("reg-shift")
            .ok_or(DwApbConfigError::MissingRegShift)?
            .as_usize()
            .ok_or(DwApbConfigError::InvalidRegShift)?;
        let reg_io_width_bytes = fdt_node
            .property("reg-io-width")
            .ok_or(DwApbConfigError::MissingRegIoWidth)?
            .as_usize()
            .ok_or(DwApbConfigError::InvalidRegIoWidth)?;

        Self::validate(
            status,
            reg_shift,
            reg_io_width_bytes,
            reg.starting_address as usize,
            reg_size_bytes,
        )
    }

    fn validate(
        status: Option<&str>,
        reg_shift: usize,
        reg_io_width_bytes: usize,
        reg_base: usize,
        reg_size_bytes: usize,
    ) -> Result<Self, DwApbConfigError> {
        if !matches!(status, None | Some("ok" | "okay")) {
            return Err(DwApbConfigError::Disabled);
        }

        if reg_shift != REGISTER_SHIFT {
            return Err(DwApbConfigError::UnsupportedRegShift);
        }

        if reg_io_width_bytes != REGISTER_IO_WIDTH_BYTES {
            return Err(DwApbConfigError::UnsupportedRegIoWidth);
        }

        if reg_size_bytes < REQUIRED_MMIO_SIZE_BYTES {
            return Err(DwApbConfigError::MmioRangeTooSmall);
        }

        let reg_end = reg_base
            .checked_add(reg_size_bytes)
            .ok_or(DwApbConfigError::MmioRangeOverflow)?;

        Ok(Self {
            mmio_range: reg_base..reg_end,
        })
    }

    fn mmio_range(&self) -> Range<usize> {
        self.mmio_range.clone()
    }
}

struct IoMemAccess {
    io_mem: IoMem,
}

impl DwApbAccess for IoMemAccess {
    fn fence(&self) {
        io_mem::fence();
    }

    fn read32(&self, offset_bytes: usize) -> Result<u32, ()> {
        self.io_mem.read_once(offset_bytes).map_err(|_| ())
    }

    fn write32(&self, offset_bytes: usize, value: u32) -> Result<(), ()> {
        self.io_mem.write_once(offset_bytes, &value).map_err(|_| ())
    }
}

#[cfg(ktest)]
mod tests {
    use alloc::{collections::VecDeque, sync::Arc, vec, vec::Vec};

    use ostd::prelude::*;
    use spin::Mutex;

    use super::*;

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum Operation {
        Fence,
        Read32(usize),
        Write32(usize, u32),
    }

    struct ScriptState {
        reads: VecDeque<Result<u32, ()>>,
        write_results: VecDeque<Result<(), ()>>,
        operations: Vec<Operation>,
    }

    #[derive(Clone)]
    struct ScriptedAccess {
        state: Arc<Mutex<ScriptState>>,
    }

    impl ScriptedAccess {
        fn new(reads: impl IntoIterator<Item = Result<u32, ()>>) -> Self {
            Self {
                state: Arc::new(Mutex::new(ScriptState {
                    reads: reads.into_iter().collect(),
                    write_results: VecDeque::new(),
                    operations: Vec::new(),
                })),
            }
        }

        fn with_write_results(self, results: impl IntoIterator<Item = Result<(), ()>>) -> Self {
            self.state.lock().write_results = results.into_iter().collect();
            self
        }

        fn operations(&self) -> Vec<Operation> {
            self.state.lock().operations.clone()
        }

        fn reads(&self) -> Vec<usize> {
            self.operations()
                .into_iter()
                .filter_map(|operation| match operation {
                    Operation::Fence => None,
                    Operation::Read32(offset_bytes) => Some(offset_bytes),
                    Operation::Write32(_, _) => None,
                })
                .collect()
        }

        fn writes(&self) -> Vec<(usize, u32)> {
            self.operations()
                .into_iter()
                .filter_map(|operation| match operation {
                    Operation::Fence => None,
                    Operation::Read32(_) => None,
                    Operation::Write32(offset_bytes, value) => Some((offset_bytes, value)),
                })
                .collect()
        }
    }

    impl DwApbAccess for ScriptedAccess {
        fn fence(&self) {
            self.state.lock().operations.push(Operation::Fence);
        }

        fn read32(&self, offset_bytes: usize) -> Result<u32, ()> {
            let mut state = self.state.lock();
            state.operations.push(Operation::Read32(offset_bytes));
            state.reads.pop_front().expect("unexpected register read")
        }

        fn write32(&self, offset_bytes: usize, value: u32) -> Result<(), ()> {
            let mut state = self.state.lock();
            state
                .operations
                .push(Operation::Write32(offset_bytes, value));
            state.write_results.pop_front().unwrap_or(Ok(()))
        }
    }

    fn validate_config(
        status: Option<&str>,
        reg_base: usize,
        reg_size_bytes: usize,
    ) -> Result<DwApbConfig, DwApbConfigError> {
        DwApbConfig::validate(
            status,
            REGISTER_SHIFT,
            REGISTER_IO_WIDTH_BYTES,
            reg_base,
            reg_size_bytes,
        )
    }

    #[ktest]
    fn dw_apb_console_send_fences_the_atomic_ownership_handoff() {
        let access = ScriptedAccess::new([Ok(LSR_THRE)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        uart.send(b"A");

        assert_eq!(
            observation.operations(),
            vec![
                Operation::Fence,
                Operation::Read32(LINE_STATUS_OFFSET_BYTES),
                Operation::Write32(TRANSMIT_HOLDING_OFFSET_BYTES, u32::from(b'A')),
                Operation::Fence,
            ]
        );
    }

    #[ktest]
    fn dw_apb_probe_reads_only_lsr() {
        let access = ScriptedAccess::new([Ok(LSR_THRE)]);

        assert_eq!(probe_ready(&access, Duration::from_secs(1)), Ok(()));
        assert_eq!(
            access.operations(),
            vec![Operation::Read32(LINE_STATUS_OFFSET_BYTES)]
        );
    }

    #[ktest]
    fn dw_apb_send_uses_shifted_u32_access_and_crlf() {
        let access = ScriptedAccess::new([Ok(LSR_THRE), Ok(LSR_THRE), Ok(LSR_THRE)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        assert_eq!(uart.try_send(b"A\n"), Ok(()));
        assert_eq!(
            observation.writes(),
            vec![
                (TRANSMIT_HOLDING_OFFSET_BYTES, 0x41),
                (TRANSMIT_HOLDING_OFFSET_BYTES, 0x0d),
                (TRANSMIT_HOLDING_OFFSET_BYTES, 0x0a)
            ]
        );
        assert_eq!(observation.reads(), vec![LINE_STATUS_OFFSET_BYTES; 3]);
    }

    #[ktest]
    fn dw_apb_send_waits_for_each_byte() {
        let access = ScriptedAccess::new([Ok(0), Ok(LSR_THRE), Ok(0), Ok(LSR_THRE)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        assert_eq!(
            uart.try_send_with_timeout(b"AB", Duration::from_secs(1)),
            Ok(())
        );
        assert_eq!(
            observation.writes(),
            vec![
                (TRANSMIT_HOLDING_OFFSET_BYTES, u32::from(b'A')),
                (TRANSMIT_HOLDING_OFFSET_BYTES, u32::from(b'B'))
            ]
        );
    }

    #[ktest]
    fn dw_apb_send_times_out_when_a_byte_never_becomes_ready() {
        let access = ScriptedAccess::new([Ok(0)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        assert_eq!(
            uart.try_send_with_timeout(b"A", Duration::ZERO),
            Err(TxError::TimedOut)
        );
        assert_eq!(observation.reads(), vec![LINE_STATUS_OFFSET_BYTES]);
        assert!(observation.writes().is_empty());
    }

    #[ktest]
    fn dw_apb_send_propagates_a_read_failure() {
        let uart = DwApbUart::new(ScriptedAccess::new([Err(())]));

        assert_eq!(uart.try_send(b"A"), Err(TxError::Mmio));
    }

    #[ktest]
    fn dw_apb_send_propagates_a_write_failure() {
        let access = ScriptedAccess::new([Ok(LSR_THRE)]).with_write_results([Err(())]);
        let uart = DwApbUart::new(access);

        assert_eq!(uart.try_send(b"A"), Err(TxError::Mmio));
    }

    #[ktest]
    fn dw_apb_console_failure_releases_ownership_before_error_handling() {
        let access = ScriptedAccess::new([Err(())]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        assert_eq!(uart.failure_state.load(Ordering::Relaxed), TX_HEALTHY);
        assert_eq!(uart.try_send(b"A"), Err(TxError::Mmio));
        assert!(uart.tx_lock.try_lock().is_some());
        assert_eq!(uart.failure_state.load(Ordering::Relaxed), TX_REPORTING);
        assert_eq!(uart.try_send(b"B"), Err(TxError::Unavailable));
        uart.mark_failure_reported();
        assert_eq!(uart.failure_state.load(Ordering::Relaxed), TX_REPORTED);
        assert_eq!(
            observation.operations(),
            vec![
                Operation::Fence,
                Operation::Read32(LINE_STATUS_OFFSET_BYTES),
                Operation::Fence
            ]
        );
    }

    #[ktest]
    fn dw_apb_console_transmits_with_preemption_disabled() {
        let access = ScriptedAccess::new([Ok(LSR_THRE)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        let _guard = ostd::task::disable_preempt();
        assert_eq!(uart.try_send(b"A"), Ok(()));
        assert_eq!(
            observation.writes(),
            vec![(TRANSMIT_HOLDING_OFFSET_BYTES, u32::from(b'A'))]
        );
    }

    #[ktest]
    fn dw_apb_console_disables_itself_after_a_runtime_timeout() {
        let access = ScriptedAccess::new([Ok(0)]);
        let observation = access.clone();
        let uart = DwApbUart::new(access);

        assert_eq!(
            uart.try_send_with_timeout(b"A", Duration::ZERO),
            Err(TxError::TimedOut)
        );
        assert_eq!(uart.failure_state.load(Ordering::Relaxed), TX_REPORTING);
        assert_eq!(
            uart.try_send_with_timeout(b"B", Duration::ZERO),
            Err(TxError::Unavailable)
        );
        assert_eq!(
            observation.operations(),
            vec![
                Operation::Fence,
                Operation::Read32(LINE_STATUS_OFFSET_BYTES),
                Operation::Fence
            ]
        );
    }

    #[ktest]
    fn dw_apb_registration_probe_accepts_ready_hardware_without_writing() {
        let access = ScriptedAccess::new([Ok(0), Ok(0), Ok(LSR_THRE)]);
        let observation = access.clone();

        assert!(prepare_for_registration(access, Duration::from_secs(1)).is_ok());
        assert_eq!(
            observation.operations(),
            vec![
                Operation::Read32(LINE_CONTROL_OFFSET_BYTES),
                Operation::Read32(INTERRUPT_ENABLE_OFFSET_BYTES),
                Operation::Read32(LINE_STATUS_OFFSET_BYTES)
            ]
        );
    }

    #[ktest]
    fn dw_apb_registration_probe_rejects_stuck_hardware_at_the_bound() {
        let access = ScriptedAccess::new([Ok(0), Ok(0), Ok(0)]);
        let observation = access.clone();

        assert_eq!(
            prepare_for_registration(access, Duration::ZERO).err(),
            Some(RegistrationError::Tx(TxError::TimedOut))
        );
        assert_eq!(
            observation.reads(),
            vec![
                LINE_CONTROL_OFFSET_BYTES,
                INTERRUPT_ENABLE_OFFSET_BYTES,
                LINE_STATUS_OFFSET_BYTES
            ]
        );
        assert!(observation.writes().is_empty());
    }

    #[ktest]
    fn dw_apb_registration_probe_rejects_an_mmio_error() {
        let access = ScriptedAccess::new([Err(())]);
        let observation = access.clone();

        assert_eq!(
            prepare_for_registration(access, Duration::from_secs(1)).err(),
            Some(RegistrationError::Tx(TxError::Mmio))
        );
        assert_eq!(
            observation.operations(),
            vec![Operation::Read32(LINE_CONTROL_OFFSET_BYTES)]
        );
    }

    #[ktest]
    fn dw_apb_registration_rejects_divisor_latch_access() {
        let access = ScriptedAccess::new([Ok(LCR_DLAB)]);

        assert_eq!(
            prepare_for_registration(access, Duration::from_secs(1)).err(),
            Some(RegistrationError::UnsupportedConfiguration)
        );
    }

    #[ktest]
    fn dw_apb_registration_rejects_programmable_thre_mode() {
        let access = ScriptedAccess::new([Ok(0), Ok(IER_PTIME)]);

        assert_eq!(
            prepare_for_registration(access, Duration::from_secs(1)).err(),
            Some(RegistrationError::UnsupportedConfiguration)
        );
    }

    #[ktest]
    fn dw_apb_accepts_the_megrez_contract() {
        let config = DwApbConfig::validate(Some("okay"), 2, 4, 0x5090_0000, 0x1_0000).unwrap();

        assert_eq!(config.mmio_range(), 0x5090_0000..0x5091_0000);
        assert_eq!(TRANSMIT_HOLDING_OFFSET_BYTES, 0);
        assert_eq!(LINE_STATUS_OFFSET_BYTES, 0x14);
    }

    #[ktest]
    fn dw_apb_accepts_absent_and_enabled_status() {
        assert!(validate_config(None, 0x1000, REQUIRED_MMIO_SIZE_BYTES).is_ok());
        assert!(validate_config(Some("ok"), 0x1000, REQUIRED_MMIO_SIZE_BYTES).is_ok());
    }

    #[ktest]
    fn dw_apb_rejects_unsupported_access_layout() {
        assert_eq!(
            DwApbConfig::validate(None, 0, REGISTER_IO_WIDTH_BYTES, 0x1000, 0x18),
            Err(DwApbConfigError::UnsupportedRegShift)
        );
        assert_eq!(
            DwApbConfig::validate(None, REGISTER_SHIFT, 1, 0x1000, 0x18),
            Err(DwApbConfigError::UnsupportedRegIoWidth)
        );
    }

    #[ktest]
    fn dw_apb_rejects_disabled_short_and_overflowing_ranges() {
        assert_eq!(
            validate_config(Some("disabled"), 0x1000, REQUIRED_MMIO_SIZE_BYTES),
            Err(DwApbConfigError::Disabled)
        );
        assert_eq!(
            validate_config(None, 0x1000, REQUIRED_MMIO_SIZE_BYTES - 1),
            Err(DwApbConfigError::MmioRangeTooSmall)
        );
        assert_eq!(
            validate_config(
                None,
                usize::MAX - REQUIRED_MMIO_SIZE_BYTES + 1,
                REQUIRED_MMIO_SIZE_BYTES
            ),
            Err(DwApbConfigError::MmioRangeOverflow)
        );
    }
}
