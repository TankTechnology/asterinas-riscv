// SPDX-License-Identifier: MPL-2.0

//! UART adapters for the generic console and TTY layers.
//!
//! Architecture backends implement [`Uart`].
//! [`UartConsole`] exposes them as console devices.
//! It coordinates input callbacks, fallible TTY output, and output-readiness notifications.

use alloc::{sync::Arc, vec::Vec};
use core::{
    fmt::Debug,
    sync::atomic::{AtomicBool, Ordering},
};

use aster_console::{
    AnyConsoleDevice, ConsoleOutputChangeCallback, ConsoleReceiveCallback, ConsoleSendError,
    ConsoleSendReadyError, InputConsoleDevice, SetOutputChangeCallbackError, TtyConsoleDevice,
};
use inherit_methods_macro::inherit_methods;
use ostd::{
    console::uart_ns16650a::{Ns16550aAccess, Ns16550aUart},
    mm::VmReader,
    sync::{LocalIrqDisabled, SpinLock},
};
use spin::Once;

const INPUT_BATCH_SIZE: usize = 16;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum DiagnosticSendError {
    Io,
}

pub(super) fn handle_fatal_diagnostic_error(error: DiagnosticSendError) -> ! {
    match error {
        DiagnosticSendError::Io => ostd::early_println!("fatal UART MMIO failure"),
    }
    ostd::panic::print_stack_trace();
    ostd::power::emergency_restart(ostd::power::ExitCode::Failure);
}

/// A UART console.
pub(super) struct UartConsole<U: Uart> {
    uart: U,
    receive_callback_fns: SpinLock<Vec<&'static ConsoleReceiveCallback>, LocalIrqDisabled>,
    output_change_callback_fn: Once<&'static ConsoleOutputChangeCallback>,
    has_pending_send_ready_notification: AtomicBool,
}

impl<U: Uart> Debug for UartConsole<U> {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("UartConsole").finish_non_exhaustive()
    }
}

impl<U: Uart> UartConsole<U> {
    /// Creates a new UART console.
    pub(super) fn new(uart: U) -> Arc<Self> {
        Arc::new(Self {
            uart,
            receive_callback_fns: SpinLock::new(Vec::new()),
            output_change_callback_fn: Once::new(),
            has_pending_send_ready_notification: AtomicBool::new(false),
        })
    }

    /// Returns a reference to the UART instance.
    #[cfg_attr(not(target_arch = "riscv64"), expect(dead_code))]
    pub(super) fn uart(&self) -> &U {
        &self.uart
    }

    // Triggers the registered input callbacks.
    pub(super) fn trigger_input_callbacks(&self) {
        let _ = self.trigger_input_callbacks_bounded(usize::MAX);
    }

    // The caller has observed a device readiness event and does not need to
    // claim the UART or poll it again from interrupt context.
    pub(super) fn notify_send_ready(&self) {
        if !self
            .has_pending_send_ready_notification
            .swap(false, Ordering::AcqRel)
        {
            return;
        }

        self.notify_output_change();
    }

    fn notify_output_change(&self) {
        if let Some(callback_fn) = self.output_change_callback_fn.get() {
            callback_fn();
        }
    }

    fn poll_uart_send_ready(&self) -> Result<bool, ConsoleSendReadyError> {
        self.uart.poll_send_ready().inspect_err(|_| {
            // A waiter may have armed while another task owned the transmitter.
            // Wake it so that it can observe the device error instead of sleeping forever.
            self.notify_output_change();
        })
    }

    // Triggers the registered input callbacks without monopolizing an interrupt handler.
    #[must_use]
    pub(super) fn trigger_input_callbacks_bounded(&self, batch_budget: usize) -> bool {
        if batch_budget == 0 {
            return false;
        }

        let mut buf = [0; INPUT_BATCH_SIZE];
        let receive_callback_fns = self.receive_callback_fns.lock().clone();

        for _ in 0..batch_budget {
            let num_rcv = self.uart.recv(&mut buf);
            if num_rcv == 0 {
                return false;
            }

            let reader = VmReader::from(&buf[..num_rcv]);
            for callback_fn in &receive_callback_fns {
                (callback_fn)(reader.clone());
            }

            if num_rcv < buf.len() {
                return false;
            }
        }

        true
    }

    fn notify_send_ready_if_pending(&self) -> Result<bool, ConsoleSendReadyError> {
        if !self
            .has_pending_send_ready_notification
            .load(Ordering::Acquire)
            || !self.poll_uart_send_ready()?
        {
            return Ok(false);
        }
        self.notify_send_ready();
        Ok(true)
    }

    fn arm_send_ready_notification(&self) -> Result<bool, ConsoleSendReadyError> {
        self.has_pending_send_ready_notification
            .store(true, Ordering::Release);
        let ready = self.poll_uart_send_ready()?;
        if ready {
            self.notify_send_ready();
        }
        Ok(ready)
    }
}

impl<U: Uart + Send + Sync + 'static> AnyConsoleDevice for UartConsole<U> {
    fn send_diagnostic_or_restart(&self, buf: &[u8]) {
        if let Err(error) = self.uart.try_send_diagnostic(buf) {
            handle_fatal_diagnostic_error(error);
        }
        // A diagnostic writer can consume readiness cached by a TTY poller.
        // Arm before polling so a transition back to ready cannot be lost.
        self.has_pending_send_ready_notification
            .store(true, Ordering::Release);
        match self.uart.poll_send_ready() {
            Ok(true) => self.notify_send_ready(),
            Ok(false) | Err(_) => self.notify_output_change(),
        }
    }

    fn tty(&self) -> Option<&dyn TtyConsoleDevice> {
        Some(self)
    }

    fn input(&self) -> Option<&dyn InputConsoleDevice> {
        Some(self)
    }
}

impl<U: Uart + Send + Sync + 'static> TtyConsoleDevice for UartConsole<U> {
    fn poll_send_ready(&self) -> Result<bool, ConsoleSendReadyError> {
        if self.poll_uart_send_ready()? {
            self.notify_send_ready();
            return Ok(true);
        }

        // Arm before rechecking so a concurrent send cannot release capacity
        // between the readiness check and callback request without a wakeup.
        self.arm_send_ready_notification()
    }

    fn try_send_tty(&self, buf: &[u8]) -> Result<usize, ConsoleSendError> {
        let result = self.uart.try_send_tty(buf);
        if matches!(result, Err(ConsoleSendError::Busy)) {
            // Another writer can consume readiness between this task's poll
            // wakeup and send attempt. Re-arm before sleeping again.
            self.arm_send_ready_notification()?;
        } else {
            let _ = self.notify_send_ready_if_pending();
        }
        result
    }

    fn try_set_output_change_callback(
        &self,
        callback_fn: &'static ConsoleOutputChangeCallback,
    ) -> Result<(), SetOutputChangeCallbackError> {
        let mut installed = false;
        self.output_change_callback_fn.call_once(|| {
            installed = true;
            callback_fn
        });
        if installed {
            Ok(())
        } else {
            Err(SetOutputChangeCallbackError::AlreadyInstalled)
        }
    }
}

impl<U: Uart + Send + Sync + 'static> InputConsoleDevice for UartConsole<U> {
    fn register_receive_callback(&self, callback_fn: &'static ConsoleReceiveCallback) {
        self.receive_callback_fns.lock().push(callback_fn);
    }
}

/// A trait that abstracts UART devices.
pub(super) trait Uart {
    /// Attempts to send diagnostic bytes to UART.
    ///
    /// This path bypasses termios, so an implementation may normalize line endings for logs.
    /// Temporary backpressure may drop diagnostic output,
    /// while device access failures are propagated to the shared fatal-error policy.
    fn try_send_diagnostic(&self, buf: &[u8]) -> Result<(), DiagnosticSendError>;

    /// Polls whether an immediate TTY send can claim the UART now.
    ///
    /// If this returns `Ok(false)`, the UART may arm a later send-readiness notification.
    fn poll_send_ready(&self) -> Result<bool, ConsoleSendReadyError> {
        Ok(true)
    }

    /// Immediately attempts to send TTY bytes and reports delivery failures.
    ///
    /// `Ok(n)` means that exactly the first `n` bytes were accepted, where `n <= buf.len()`.
    /// An error may be returned only when no byte was accepted.
    fn try_send_tty(&self, buf: &[u8]) -> Result<usize, ConsoleSendError>;

    /// Receives a sequence of bytes from UART and returns the number of received bytes.
    #[must_use]
    fn recv(&self, buf: &mut [u8]) -> usize;

    /// Flushes the received buffer.
    ///
    /// This method should be called after setting up the IRQ handlers to ensure new received data
    /// will trigger IRQs.
    fn flush(&self);
}

impl<A: Ns16550aAccess> Uart for SpinLock<Ns16550aUart<A>, LocalIrqDisabled> {
    fn try_send_diagnostic(&self, buf: &[u8]) -> Result<(), DiagnosticSendError> {
        let mut uart = self.lock();

        for byte in buf {
            // TODO: This is termios-specific behavior and should be part of the TTY implementation
            // instead of the serial console implementation. See the ONLCR flag for more details.
            if *byte == b'\n' {
                uart.send(b'\r');
            }
            uart.send(*byte);
        }

        Ok(())
    }

    fn try_send_tty(&self, buf: &[u8]) -> Result<usize, ConsoleSendError> {
        let _ = self.try_send_diagnostic(buf);
        Ok(buf.len())
    }

    fn recv(&self, buf: &mut [u8]) -> usize {
        let mut uart = self.lock();

        for (i, byte) in buf.iter_mut().enumerate() {
            let Some(recv_byte) = uart.recv() else {
                return i;
            };
            *byte = recv_byte;
        }

        buf.len()
    }

    fn flush(&self) {
        let mut uart = self.lock();

        while uart.recv().is_some() {}
    }
}

#[inherit_methods(from = "(**self)")]
impl<A: Ns16550aAccess> Uart for &SpinLock<Ns16550aUart<A>, LocalIrqDisabled> {
    fn try_send_diagnostic(&self, buf: &[u8]) -> Result<(), DiagnosticSendError>;
    fn try_send_tty(&self, buf: &[u8]) -> Result<usize, ConsoleSendError>;
    fn recv(&self, buf: &mut [u8]) -> usize;
    fn flush(&self);
}

#[cfg(ktest)]
mod tests {
    use core::sync::atomic::AtomicUsize;

    use ostd::{mm::Infallible, prelude::ktest};

    use super::*;

    static CALLBACK_BYTES: AtomicUsize = AtomicUsize::new(0);
    static NR_SEND_READY_CALLBACK_CALLS: AtomicUsize = AtomicUsize::new(0);

    fn record_input(reader: VmReader<Infallible>) {
        CALLBACK_BYTES.fetch_add(reader.remain(), Ordering::Relaxed);
    }

    static RECORD_INPUT_FN: fn(VmReader<Infallible>) = record_input;

    fn record_send_ready() {
        NR_SEND_READY_CALLBACK_CALLS.fetch_add(1, Ordering::Relaxed);
    }

    static RECORD_SEND_READY_FN: fn() = record_send_ready;

    struct LegacyUart {
        send_calls: Arc<AtomicUsize>,
    }

    enum TestUartMode {
        Ready {
            tty_send_calls: Arc<AtomicUsize>,
            receive_calls: Arc<AtomicUsize>,
        },
        BackpressureBecomesReady(Arc<AtomicBool>),
        DiagnosticConsumesCapacity(AtomicBool),
        PersistentlyBackpressured,
        BusyOnSend,
        FailingReadiness,
        ReadyOnSecondPoll(AtomicUsize),
    }

    struct TestUart(TestUartMode);

    impl TestUart {
        fn new(mode: TestUartMode) -> Self {
            Self(mode)
        }
    }

    impl Uart for LegacyUart {
        fn try_send_diagnostic(&self, _buf: &[u8]) -> Result<(), DiagnosticSendError> {
            self.send_calls.fetch_add(1, Ordering::Relaxed);
            Ok(())
        }

        fn try_send_tty(&self, buf: &[u8]) -> Result<usize, ConsoleSendError> {
            let _ = self.try_send_diagnostic(buf);
            Ok(buf.len())
        }

        fn recv(&self, _buf: &mut [u8]) -> usize {
            0
        }

        fn flush(&self) {}
    }

    impl Uart for TestUart {
        fn try_send_diagnostic(&self, _buf: &[u8]) -> Result<(), DiagnosticSendError> {
            match &self.0 {
                TestUartMode::BackpressureBecomesReady(ready) => {
                    ready.store(true, Ordering::Relaxed)
                }
                TestUartMode::DiagnosticConsumesCapacity(ready) => {
                    ready.store(false, Ordering::Relaxed)
                }
                _ => {}
            }
            Ok(())
        }

        fn poll_send_ready(&self) -> Result<bool, ConsoleSendReadyError> {
            match &self.0 {
                TestUartMode::Ready { .. } => Ok(true),
                TestUartMode::BackpressureBecomesReady(ready) => Ok(ready.load(Ordering::Relaxed)),
                TestUartMode::DiagnosticConsumesCapacity(ready) => {
                    Ok(ready.load(Ordering::Relaxed))
                }
                TestUartMode::PersistentlyBackpressured | TestUartMode::BusyOnSend => Ok(false),
                TestUartMode::FailingReadiness => Err(ConsoleSendReadyError::Io),
                TestUartMode::ReadyOnSecondPoll(poll_calls) => {
                    Ok(poll_calls.fetch_add(1, Ordering::Relaxed) != 0)
                }
            }
        }

        fn try_send_tty(&self, buf: &[u8]) -> Result<usize, ConsoleSendError> {
            match &self.0 {
                TestUartMode::Ready { tty_send_calls, .. } => {
                    tty_send_calls.fetch_add(1, Ordering::Relaxed);
                    Ok(buf.len())
                }
                TestUartMode::BusyOnSend | TestUartMode::FailingReadiness => {
                    Err(ConsoleSendError::Busy)
                }
                _ => {
                    let _ = self.try_send_diagnostic(buf);
                    Ok(buf.len())
                }
            }
        }

        fn recv(&self, buf: &mut [u8]) -> usize {
            let TestUartMode::Ready { receive_calls, .. } = &self.0 else {
                return 0;
            };
            receive_calls.fetch_add(1, Ordering::Relaxed);
            buf.fill(b'x');
            buf.len()
        }

        fn flush(&self) {}
    }

    #[ktest]
    fn console_tty_send_reaches_the_uart() {
        let tty_send_calls = Arc::new(AtomicUsize::new(0));
        let console = UartConsole::new(TestUart::new(TestUartMode::Ready {
            tty_send_calls: tty_send_calls.clone(),
            receive_calls: Arc::new(AtomicUsize::new(0)),
        }));

        assert_eq!(console.try_send_tty(b"x"), Ok(1));

        assert_eq!(tty_send_calls.load(Ordering::Relaxed), 1);
    }

    #[ktest]
    fn legacy_tty_send_uses_an_explicit_infallible_adapter() {
        let send_calls = Arc::new(AtomicUsize::new(0));
        let console = UartConsole::new(LegacyUart {
            send_calls: send_calls.clone(),
        });

        assert_eq!(console.try_send_tty(b"x"), Ok(1));
        assert_eq!(send_calls.load(Ordering::Relaxed), 1);
    }

    #[ktest]
    fn console_notifies_send_ready_after_a_send_attempt() {
        NR_SEND_READY_CALLBACK_CALLS.store(0, Ordering::Relaxed);
        let console = UartConsole::new(TestUart::new(TestUartMode::BackpressureBecomesReady(
            Arc::new(AtomicBool::new(false)),
        )));
        assert!(
            console
                .try_set_output_change_callback(&RECORD_SEND_READY_FN)
                .is_ok()
        );

        assert_eq!(console.poll_send_ready(), Ok(false));
        assert_eq!(console.try_send_tty(b"x"), Ok(1));
        assert_eq!(NR_SEND_READY_CALLBACK_CALLS.load(Ordering::Relaxed), 1);
    }

    #[ktest]
    fn diagnostic_send_invalidates_observed_output_readiness() {
        NR_SEND_READY_CALLBACK_CALLS.store(0, Ordering::Relaxed);
        let console = UartConsole::new(TestUart::new(TestUartMode::DiagnosticConsumesCapacity(
            AtomicBool::new(true),
        )));
        assert!(
            console
                .try_set_output_change_callback(&RECORD_SEND_READY_FN)
                .is_ok()
        );

        assert_eq!(console.poll_send_ready(), Ok(true));
        console.send_diagnostic_or_restart(b"x");

        assert_eq!(NR_SEND_READY_CALLBACK_CALLS.load(Ordering::Relaxed), 1);
        assert_eq!(console.poll_send_ready(), Ok(false));
        console.notify_send_ready();
        assert_eq!(NR_SEND_READY_CALLBACK_CALLS.load(Ordering::Relaxed), 2);
    }

    #[ktest]
    fn console_does_not_notify_without_observed_backpressure() {
        NR_SEND_READY_CALLBACK_CALLS.store(0, Ordering::Relaxed);
        let console = UartConsole::new(LegacyUart {
            send_calls: Arc::new(AtomicUsize::new(0)),
        });
        assert!(
            console
                .try_set_output_change_callback(&RECORD_SEND_READY_FN)
                .is_ok()
        );
        assert_eq!(
            console.try_set_output_change_callback(&RECORD_SEND_READY_FN),
            Err(SetOutputChangeCallbackError::AlreadyInstalled)
        );

        assert_eq!(console.poll_send_ready(), Ok(true));
        assert_eq!(console.try_send_tty(b"x"), Ok(1));
        assert_eq!(NR_SEND_READY_CALLBACK_CALLS.load(Ordering::Relaxed), 0);
    }

    #[ktest]
    fn console_does_not_notify_while_backpressure_remains() {
        NR_SEND_READY_CALLBACK_CALLS.store(0, Ordering::Relaxed);
        let console = UartConsole::new(TestUart::new(TestUartMode::PersistentlyBackpressured));
        assert!(
            console
                .try_set_output_change_callback(&RECORD_SEND_READY_FN)
                .is_ok()
        );

        assert_eq!(console.poll_send_ready(), Ok(false));
        assert_eq!(console.try_send_tty(b"x"), Ok(1));
        assert_eq!(NR_SEND_READY_CALLBACK_CALLS.load(Ordering::Relaxed), 0);
    }

    #[ktest]
    fn device_readiness_notification_does_not_repoll_the_uart() {
        NR_SEND_READY_CALLBACK_CALLS.store(0, Ordering::Relaxed);
        let console = UartConsole::new(TestUart::new(TestUartMode::PersistentlyBackpressured));
        assert!(
            console
                .try_set_output_change_callback(&RECORD_SEND_READY_FN)
                .is_ok()
        );

        assert_eq!(console.poll_send_ready(), Ok(false));
        console.notify_send_ready();

        assert_eq!(NR_SEND_READY_CALLBACK_CALLS.load(Ordering::Relaxed), 1);
    }

    #[ktest]
    fn busy_send_rearms_a_consumed_readiness_notification() {
        NR_SEND_READY_CALLBACK_CALLS.store(0, Ordering::Relaxed);
        let console = UartConsole::new(TestUart::new(TestUartMode::BusyOnSend));
        assert!(
            console
                .try_set_output_change_callback(&RECORD_SEND_READY_FN)
                .is_ok()
        );

        assert_eq!(console.poll_send_ready(), Ok(false));
        console.notify_send_ready();
        assert_eq!(NR_SEND_READY_CALLBACK_CALLS.load(Ordering::Relaxed), 1);

        assert_eq!(console.try_send_tty(b"x"), Err(ConsoleSendError::Busy));
        console.notify_send_ready();
        assert_eq!(NR_SEND_READY_CALLBACK_CALLS.load(Ordering::Relaxed), 2);
    }

    #[ktest]
    fn readiness_failure_turns_a_busy_send_into_an_io_error() {
        let console = UartConsole::new(TestUart::new(TestUartMode::FailingReadiness));

        assert_eq!(console.try_send_tty(b"x"), Err(ConsoleSendError::Io));
        assert_eq!(console.poll_send_ready(), Err(ConsoleSendReadyError::Io));
    }

    #[ktest]
    fn readiness_failure_notifies_an_armed_waiter() {
        NR_SEND_READY_CALLBACK_CALLS.store(0, Ordering::Relaxed);
        let console = UartConsole::new(TestUart::new(TestUartMode::FailingReadiness));
        assert!(
            console
                .try_set_output_change_callback(&RECORD_SEND_READY_FN)
                .is_ok()
        );
        console
            .has_pending_send_ready_notification
            .store(true, Ordering::Release);

        assert_eq!(console.poll_send_ready(), Err(ConsoleSendReadyError::Io));
        assert_eq!(NR_SEND_READY_CALLBACK_CALLS.load(Ordering::Relaxed), 1);
    }

    #[ktest]
    fn successful_recheck_notifies_an_armed_waiter() {
        NR_SEND_READY_CALLBACK_CALLS.store(0, Ordering::Relaxed);
        let console = UartConsole::new(TestUart::new(TestUartMode::ReadyOnSecondPoll(
            AtomicUsize::new(0),
        )));
        assert!(
            console
                .try_set_output_change_callback(&RECORD_SEND_READY_FN)
                .is_ok()
        );

        assert_eq!(console.poll_send_ready(), Ok(true));
        assert_eq!(NR_SEND_READY_CALLBACK_CALLS.load(Ordering::Relaxed), 1);
    }

    #[ktest]
    fn fast_ready_poll_notifies_a_concurrent_waiter() {
        NR_SEND_READY_CALLBACK_CALLS.store(0, Ordering::Relaxed);
        let console = UartConsole::new(LegacyUart {
            send_calls: Arc::new(AtomicUsize::new(0)),
        });
        assert!(
            console
                .try_set_output_change_callback(&RECORD_SEND_READY_FN)
                .is_ok()
        );
        console
            .has_pending_send_ready_notification
            .store(true, Ordering::Release);

        assert_eq!(console.poll_send_ready(), Ok(true));
        assert_eq!(NR_SEND_READY_CALLBACK_CALLS.load(Ordering::Relaxed), 1);
    }

    #[ktest]
    fn bounded_input_callbacks_stop_after_the_batch_budget() {
        const BATCH_BUDGET: usize = 3;

        CALLBACK_BYTES.store(0, Ordering::Relaxed);
        let receive_calls = Arc::new(AtomicUsize::new(0));
        let console = UartConsole::new(TestUart::new(TestUartMode::Ready {
            tty_send_calls: Arc::new(AtomicUsize::new(0)),
            receive_calls: receive_calls.clone(),
        }));
        console.register_receive_callback(&RECORD_INPUT_FN);

        assert!(!console.trigger_input_callbacks_bounded(0));
        assert_eq!(receive_calls.load(Ordering::Relaxed), 0);
        assert_eq!(CALLBACK_BYTES.load(Ordering::Relaxed), 0);

        assert!(console.trigger_input_callbacks_bounded(BATCH_BUDGET));

        assert_eq!(receive_calls.load(Ordering::Relaxed), BATCH_BUDGET);
        assert_eq!(
            CALLBACK_BYTES.load(Ordering::Relaxed),
            BATCH_BUDGET * INPUT_BATCH_SIZE
        );
    }
}
